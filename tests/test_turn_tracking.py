"""turn_id 追踪与压缩保护单测。"""

import pytest

from agent_runtime.compression_pipeline import l2_snip, l3_microcompact
from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager, TokenBudget
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.turn_tracking import current_turn_id, stamp_turn_id
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def budget():
    return TokenBudget(model="gpt-4", provider="openai", total_limit=6000)


def _exploration_turn(turn_id: int, *, lines: int = 50) -> list[dict]:
    body = "\n".join(f"line {turn_id}-{j} ok content here" for j in range(lines))
    return [
        {"role": "user", "content": f"explore {turn_id}", "turn_id": turn_id},
        {
            "role": "assistant",
            "content": "list_files",
            "tool_name": "list_files",
            "turn_id": turn_id,
        },
        {"role": "tool", "tool_name": "list_files", "content": body, "turn_id": turn_id},
        {"role": "assistant", "content": "read_file", "tool_name": "read_file", "turn_id": turn_id},
        {"role": "tool", "tool_name": "read_file", "content": body, "turn_id": turn_id},
    ]


class TestTurnTracking:
    def test_user_starts_new_turn_id(self):
        session: dict = {"_turn_counter": 0, "history": []}
        u1 = stamp_turn_id(session, {"role": "user", "content": "a"})
        a1 = stamp_turn_id(session, {"role": "assistant", "content": "b"})
        u2 = stamp_turn_id(session, {"role": "user", "content": "c"})
        assert u1["turn_id"] == 1
        assert a1["turn_id"] == 1
        assert u2["turn_id"] == 2
        assert current_turn_id(session) == 2

    def test_agent_record_stamps_turn_id(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        agent.record({"role": "user", "content": "hi"})
        agent.record({"role": "assistant", "content": "hello"})
        assert agent.session["history"][0]["turn_id"] == 1
        assert agent.session["history"][1]["turn_id"] == 1


class TestCurrentTurnProtection:
    def test_l2_skips_current_turn_id_even_in_middle(self, budget):
        history: list[dict] = []
        for tid in range(1, 3):
            history.extend(_exploration_turn(tid, lines=60))
        marker = "CURRENT_TURN_UNIQUE_BODY"
        history.extend(
            [
                {"role": "user", "content": "current", "turn_id": 3},
                {"role": "assistant", "tool_name": "list_files", "content": "x", "turn_id": 3},
                {
                    "role": "tool",
                    "tool_name": "list_files",
                    "content": marker + "\n" + "z" * 3000,
                    "turn_id": 3,
                },
            ]
        )
        for tid in range(4, 7):
            history.extend(_exploration_turn(tid, lines=60))

        meta: dict = {}
        result = l2_snip(
            history,
            budget,
            meta,
            history_window=2600,
            current_turn_id=3,
        )
        assert marker in "\n".join(str(i.get("content", "")) for i in result)
        assert meta["compression_pipeline"].get("l2_snipped_turns", 0) >= 1

    def test_l3_skips_current_turn_tools(self, budget):
        body = "payload " + "x" * 4000
        history = _exploration_turn(1, lines=80)
        history.extend(
            [
                {"role": "user", "content": "current", "turn_id": 2},
                {"role": "tool", "tool_name": "read_file", "content": body, "turn_id": 2},
            ]
        )
        meta: dict = {}
        result = l3_microcompact(
            history,
            budget,
            meta,
            history_window=2600,
            current_turn_id=2,
        )
        last_tool = [i for i in result if i.get("role") == "tool"][-1]
        assert not str(last_tool.get("content", "")).startswith("[ref:#")

    def test_context_manager_records_current_turn_id(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=4, prompt_budget=6000),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        agent.record({"role": "user", "content": "start"})
        cm = ContextManager(agent)
        assert cm.tier_policy.current_turn_id == 1
