"""L1/L2 State 关联字段单测。"""

from __future__ import annotations

from src.repair.l2_binding import (
    AgentAskRef,
    bind_l2_context,
    clear_l2_context,
    l2_payload_from_agent,
    make_repair_task_id,
)


class _StubAgent:
    pass


class TestMakeRepairTaskId:
    def test_first_attempt_no_suffix(self):
        rid = "550e8400-e29b-41d4-a716-446655440000"
        assert make_repair_task_id(rid, "localizer", 0) == f"{rid}-localizer"

    def test_retry_includes_attempt(self):
        rid = "550e8400-e29b-41d4-a716-446655440000"
        assert make_repair_task_id(rid, "patcher", 2) == f"{rid}-patcher-2"


class TestBindL2Context:
    def test_bind_and_clear(self):
        agent = _StubAgent()
        rid = "550e8400-e29b-41d4-a716-446655440000"
        task_id = bind_l2_context(
            agent,
            repair_run_id=rid,
            agent_name="retriever",
            phase="retrieve",
            attempt=0,
            started_ms=120,
        )
        assert task_id == f"{rid}-retriever"
        assert agent._l2_phase == "retrieve"
        payload = l2_payload_from_agent(agent)
        assert payload["l2_agent"] == "retriever"
        assert payload["repair_run_id"] == rid
        clear_l2_context(agent)
        assert l2_payload_from_agent(agent) == {}


class TestAgentAskRef:
    def test_roundtrip_dict(self):
        ref = AgentAskRef(
            agent="patcher",
            phase="patch",
            attempt=1,
            task_id="uuid-patcher-1",
            run_id="uuid",
            started_ms=100,
            finished_ms=250,
            stop_reason="final",
            tool_steps=0,
        )
        restored = AgentAskRef.from_dict(ref.to_dict())
        assert restored == ref
