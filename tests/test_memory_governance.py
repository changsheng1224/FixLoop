from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeCandidate:
    key: str
    value: str
    kind: str = "fact"
    confidence: float = 0.9
    source: str = "tool"
    evidence_refs: list[str] = field(default_factory=list)


def test_task_memory_cannot_promote_even_when_verified():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    state = {}
    service = MemoryGovernanceService(state)
    memory = service.ingest(FakeCandidate("root_cause", "foo returns None"), scope="task")
    service.bind_evidence(memory.memory_id, ["E1"])
    service.validate(memory.memory_id, passed=True)
    service.promote_eligible()
    assert service.registry[memory.memory_id]["status"] == "verified"


def test_repository_fact_promotes_only_with_evidence_and_verification():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    service = MemoryGovernanceService({})
    memory = service.ingest(
        FakeCandidate("test.command", "pytest -q", confidence=0.9),
        scope="repository",
    )
    assert service.promote_eligible() == 0
    service.bind_evidence(memory.memory_id, ["E2"])
    service.validate(memory.memory_id, passed=True)
    assert service.promote_eligible() == 1
    assert service.registry[memory.memory_id]["status"] == "durable"


def test_model_inference_needs_explicit_confirmation():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    candidate = FakeCandidate("build.command", "npm test", source="", confidence=1.0)
    service = MemoryGovernanceService({})
    memory = service.ingest(candidate, scope="repository")
    service.bind_evidence(memory.memory_id, ["E3"])
    service.validate(memory.memory_id, passed=True)
    assert service.promote_eligible() == 0
    assert service.promote_eligible(user_confirmed=True) == 1


def test_conflicts_are_withheld_from_recall():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    service = MemoryGovernanceService({})
    service.ingest(FakeCandidate("test.command", "pytest"), scope="repository")
    service.ingest(FakeCandidate("test.command", "tox"), scope="repository")
    service.consolidate()
    assert service.stats["conflicts_detected"] == 1
    assert service.recall("test command", scope="repository") == []


def test_version_memory_is_marked_stale(tmp_path):
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    service = MemoryGovernanceService({}, repo_root=str(tmp_path))
    memory = service.ingest(
        FakeCandidate("api.shape", "v1"),
        scope="repository_version",
        repo_fingerprint="old-version",
    )
    service.refresh_versions()
    assert service.registry[memory.memory_id]["status"] == "stale"


def test_recall_prefers_current_scope_and_emits_audit():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    state = {}
    service = MemoryGovernanceService(state)
    task = service.ingest(FakeCandidate("pytest", "pytest target"), scope="task")
    repo = service.ingest(FakeCandidate("pytest", "pytest project"), scope="repository")
    service.bind_evidence(task.memory_id, ["E1"])
    service.bind_evidence(repo.memory_id, ["E2"])
    results = service.recall("pytest", limit=2)
    assert results[0]["scope"] == "task"
    assert state["memory_governance_audit"]


def test_memory_dream_reports_governance_stats():
    from agent_runtime.features.memory.core import default_memory_state
    from agent_runtime.features.memory.dream import run_memory_dream

    stats, _ = run_memory_dream(default_memory_state())
    assert "conflicts_detected" in stats
    assert "stale_marked" in stats


def test_recall_enforces_user_and_task_isolation():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    state = {}
    alice = MemoryGovernanceService(state, user_id="alice", task_id="task-a")
    user_memory = alice.ingest(FakeCandidate("style", "use pytest"), scope="user")
    task_memory = alice.ingest(FakeCandidate("failure", "pytest timeout"), scope="task")

    bob = MemoryGovernanceService(state, user_id="bob", task_id="task-b")
    assert bob.recall("pytest") == []
    assert user_memory.memory_id in {
        item["memory_id"] for item in alice.recall("pytest", task_id="task-b")
    }
    assert task_memory.memory_id not in {
        item["memory_id"] for item in alice.recall("pytest", task_id="task-b")
    }


def test_usage_feedback_changes_quality_and_demotes():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    service = MemoryGovernanceService({}, task_id="task-a")
    memory = service.ingest(FakeCandidate("pytest", "pytest -q"), scope="task")
    before = service.registry[memory.memory_id]["confidence"]
    assert service.record_usage(memory.memory_id, outcome="fixed", task_id="task-a")
    assert service.registry[memory.memory_id]["confidence"] > before
    for _ in range(3):
        assert service.record_usage(memory.memory_id, outcome="failed", task_id="task-a")
    assert service.registry[memory.memory_id]["status"] == "demoted"


def test_recall_prefers_successful_recent_memory():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    service = MemoryGovernanceService({}, task_id="t")
    weak = service.ingest(FakeCandidate("pytest", "pytest command"), scope="task")
    strong = service.ingest(FakeCandidate("pytest", "pytest target command"), scope="task")
    service.record_usage(strong.memory_id, outcome="fixed")
    results = service.recall("pytest target command")
    assert results[0]["memory_id"] == strong.memory_id
    assert weak.memory_id in {item["memory_id"] for item in results}


def test_context_runtime_selects_provenance_and_never_replays_writes():
    from agent_runtime.context_runtime import (
        ContextItem,
        ContextPolicyEngine,
        ContextRequest,
        append_action,
        build_action_record,
        replay_policy,
    )

    request = ContextRequest(phase="patch", active_hypothesis_ids=["H-1"], token_budget=10)
    items = [
        ContextItem("low", "memory", "old", token_cost=8, relevance=0.2),
        ContextItem(
            "high", "observation", "evidence", source_ref="OBS-1", token_cost=3,
            relevance=0.9, confidence=0.9, evidence_strength=1.0, hypothesis_ids=["H-1"],
        ),
    ]
    selected = ContextPolicyEngine().select(items, request)
    assert [item.item_id for item in selected] == ["high"]

    state = {}
    action = build_action_record("apply_patch", {"path": "a.py"}, revision=1, side_effect="write")
    append_action(state, action)
    assert replay_policy(state, "apply_patch", {"path": "a.py"}) == "never_replay"
