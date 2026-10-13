"""The agent's history projection is served by the durable log, not by the
in-memory session list.

Layer 2 used to compact `session["history"]` between patcher attempts and
report the characters it "dropped". Every prompt build reads the append-only
JSONL instead, so the compaction never reached the model while its metrics
did. These tests pin the property that made a session-list rewrite inert, and
pin the removal of the config switch that drove it.
"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.state_root import state_root_for


@pytest.fixture
def agent(workspace):
    return Agent(
        config=AgentConfig(provider="fake", max_steps=2, approval="auto"),
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=workspace,
    )


def history_path(agent):
    return state_root_for(agent.cwd, agent.tool_context.state_root) / ".agent" / "history.jsonl"


class TestHistoryProjection:
    def test_recording_dual_writes_the_session_list_and_the_log(self, agent):
        agent.record({"role": "user", "content": "hello"})
        assert any(item.get("content") == "hello" for item in agent.session["history"])
        assert "hello" in history_path(agent).read_text(encoding="utf-8")

    def test_the_log_wins_when_it_exists(self, agent):
        agent.record({"role": "user", "content": "durable"})
        agent.session["history"] = [{"role": "user", "content": "in-memory-only"}]

        contents = [item.get("content") for item in agent.read_history()]
        assert "durable" in contents
        assert "in-memory-only" not in contents

    def test_rewriting_the_session_list_cannot_change_what_the_model_sees(self, agent):
        """The exact operation the removed Layer 2 compaction performed."""
        agent.record({"role": "user", "content": "x" * 5000})
        before = [item.get("content") for item in agent.read_history()]

        agent.session["history"] = [
            {**item, "content": "...[compacted]..."} for item in agent.session["history"]
        ]
        assert [item.get("content") for item in agent.read_history()] == before

    def test_the_session_list_is_the_fallback_without_a_log(self, agent):
        path = history_path(agent)
        if path.exists():
            path.unlink()
        agent.session["history"] = [{"role": "user", "content": "fallback"}]
        assert [item.get("content") for item in agent.read_history()] == ["fallback"]


class TestCompactionSwitchIsGone:
    def test_repair_config_rejects_the_removed_key(self):
        from pydantic import ValidationError

        from src.repair.config import RepairConfig

        with pytest.raises(ValidationError):
            RepairConfig(patcher_compact=True)

    def test_repair_config_no_longer_declares_it(self):
        from src.repair.config import RepairConfig

        assert "patcher_compact" not in RepairConfig.model_fields

    def test_the_module_is_gone(self):
        with pytest.raises(ImportError):
            __import__("src.repair.context_compact")
