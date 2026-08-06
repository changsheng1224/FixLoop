"""L1/L2 State 关联字段单测。"""

from __future__ import annotations

from agent_runtime.l2_context import l2_payload_from_agent
from src.repair.l2_binding import bind_l2_context, clear_l2_context, make_repair_task_id
from src.state import AgentAskRef


class _StubAgent:
    pass


class TestMakeRepairTaskId:
    def test_first_attempt_no_suffix(self):
        rid = "550e8400-e29b-41d4-a716-446655440000"
        assert make_repair_task_id(rid, "patcher", 0) == f"{rid}-patcher"

    def test_retry_includes_attempt(self):
        rid = "550e8400-e29b-41d4-a716-446655440000"
        assert make_repair_task_id(rid, "patcher", 2) == f"{rid}-patcher-2"


class TestBindL2Context:
    def test_bind_and_clear(self):
        agent = _StubAgent()
        task_id = bind_l2_context(
            agent,
            repair_run_id="run-1",
            agent_name="patcher",
            phase="patch",
            attempt=0,
            started_ms=100,
        )
        assert task_id == "run-1-patcher"
        assert agent._l2_agent == "patcher"
        clear_l2_context(agent)
        assert not hasattr(agent, "_l2_agent")

    def test_payload_from_agent(self):
        agent = _StubAgent()
        bind_l2_context(
            agent,
            repair_run_id="run-1",
            agent_name="patcher",
            phase="patch",
            attempt=1,
            started_ms=0,
        )
        payload = l2_payload_from_agent(agent)
        assert payload["l2_agent"] == "patcher"
        assert payload["l2_attempt"] == 1
        clear_l2_context(agent)
        assert l2_payload_from_agent(agent) == {}


class TestAgentAskRef:
    def test_round_trip_dict(self):
        ref = AgentAskRef(
            agent="patcher",
            phase="patch",
            attempt=2,
            task_id="run-patcher-2",
            run_id="run",
            started_ms=10,
            finished_ms=20,
            stop_reason="complete_once",
            tool_steps=0,
        )
        restored = AgentAskRef.from_dict(ref.to_dict())
        assert restored == ref
