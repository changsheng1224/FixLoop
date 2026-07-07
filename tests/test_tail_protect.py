"""尾部 20k token 保护区（L2–L4 整 turn 豁免）单测。"""

import pytest

from agent_runtime.compression_pipeline import (
    TAIL_PROTECT_TOKENS,
    effective_tail_protect_tokens,
    l3_microcompact,
    protected_turn_indices,
    group_history_into_turns,
    count_history_tokens,
)
from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager, TokenBudget, history_window_budget
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def budget():
    return TokenBudget(model="gpt-4", provider="openai", total_limit=6000)


def _fat_tool_turn(label: str, *, pad_lines: int = 200) -> list[dict]:
    body = "\n".join(f"{label}-line-{j} content padding" for j in range(pad_lines))
    return [
        {"role": "user", "content": f"task {label}"},
        {"role": "assistant", "tool_name": "read_file", "content": "read"},
        {"role": "tool", "tool_name": "read_file", "content": body},
    ]


class TestEffectiveTailProtect:
    def test_small_window_uses_legacy_ratio_cap(self):
        assert effective_tail_protect_tokens(20_000, 2600) == 2000

    def test_full_20k_for_large_window(self):
        window = history_window_budget(100_000)
        assert effective_tail_protect_tokens(20_000, window) == 20_000


class TestProtectedTurnIndices:
    def test_tail_protect_leaves_old_turn_unprotected(self, budget):
        old_marker = "OLD_TURN_OUTSIDE_TAIL"
        old = _fat_tool_turn("old", pad_lines=400)
        old[-1]["content"] = old_marker + "\n" + old[-1]["content"]
        boundary = _fat_tool_turn("boundary", pad_lines=250)
        recent = _fat_tool_turn("recent", pad_lines=40)

        history = old + boundary + recent
        turns = group_history_into_turns(history)
        protected = protected_turn_indices(
            turns,
            budget,
            history_window=2600,
            tail_protect_tokens=20_000,
        )
        assert 0 not in protected
        assert len(protected) >= 1
        assert len(protected) < len(turns)

    def test_l3_skips_tools_in_tail_zone(self, budget):
        old = _fat_tool_turn("old", pad_lines=500)
        middle = _fat_tool_turn("middle", pad_lines=220)
        tail = _fat_tool_turn("tail", pad_lines=80)
        marker = "TAIL_TOOL_KEEP_ME"
        tail[-1]["content"] = marker + "\n" + tail[-1]["content"]
        history = old + middle + tail

        meta: dict = {}
        result = l3_microcompact(
            history,
            budget,
            meta,
            history_window=2600,
            tail_protect_tokens=20_000,
        )
        assert marker in "\n".join(str(i.get("content", "")) for i in result)
        assert meta["compression_pipeline"].get("l3_compacted", 0) >= 1


class TestTailProtectConfig:
    def test_agent_config_default(self):
        cfg = AgentConfig()
        assert cfg.tail_protect_tokens == TAIL_PROTECT_TOKENS

    def test_context_manager_passes_tail_protect(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2, tail_protect_tokens=15_000),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        cm = ContextManager(agent)
        assert cm.tier_policy.tail_protect_tokens == 15_000

    def test_pipeline_metadata_records_tail_protect(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=4, prompt_budget=6000),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        agent.record({"role": "user", "content": "hi"})
        cm = ContextManager(agent)
        _, meta = cm.build("next")
        pipe = meta.get("compression_pipeline", {})
        assert pipe.get("tail_protect_tokens") == TAIL_PROTECT_TOKENS
        assert pipe["tail_protect_effective"] == 2000
