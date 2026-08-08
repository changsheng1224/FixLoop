from dataclasses import dataclass


@dataclass
class Candidate:
    key: str
    value: str
    source: str = "agent"
    source_type: str = "model"
    scope: str = "task"
    confidence: float = 0.9
    evidence_refs: list[str] | None = None


def test_recall_records_event_without_verifying_memory():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    state = {}
    service = MemoryGovernanceService(state, task_id="task-1")
    memory = service.ingest(Candidate("framework", "pytest"), scope="task")

    service.record_recall([memory.memory_id], task_id="task-1", trace_id="trace-1")

    assert state["recalled_memory_ids"] == [memory.memory_id]
    event = state["memory_usage_events"][-1]
    assert event["usage"] == "recalled"
    assert event["outcome"] == "inconclusive"
    assert service.registry[memory.memory_id]["verification_count"] == 0


def test_inconclusive_feedback_does_not_penalize_memory():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    service = MemoryGovernanceService({}, task_id="task-1")
    memory = service.ingest(Candidate("framework", "pytest"), scope="task")
    before = service.registry[memory.memory_id]["confidence"]

    assert service.record_usage(
        memory.memory_id,
        outcome="inconclusive",
        evidence_refs=["OBS-verify"],
        task_id="task-1",
    )

    raw = service.registry[memory.memory_id]
    assert raw["confidence"] == before
    assert raw["usage_failures"] == 0
    assert service.stats["usage_inconclusive"] == 1


def test_dream_records_successful_run_state():
    from agent_runtime.features.memory.dream import MemoryDreamer

    state = {"episodic_notes": []}
    stats = MemoryDreamer(state).run()

    assert stats["total_before"] == 0
    assert state["memory_dream"]["status"] == "succeeded"
    assert state["memory_dream"]["stats"]["total_after"] == 0
