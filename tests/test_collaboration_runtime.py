from __future__ import annotations

import time

import pytest


def test_task_contract_and_dag_dependency_release():
    from src.collaboration.contracts import AgentTask, TaskStatus
    from src.collaboration.dag import TaskDAG, TaskDAGError

    dag = TaskDAG()
    first = AgentTask(task_id="t1", run_id="r1", role="localizer", kind="loc")
    second = AgentTask(task_id="t2", run_id="r1", role="patcher", kind="patch", depends_on=["t1"])
    dag.add(first)
    dag.add(second)
    assert [task.task_id for task in dag.ready()] == ["t1"]
    dag.transition("t1", TaskStatus.RUNNING)
    dag.transition("t1", TaskStatus.COMPLETED)
    assert [task.task_id for task in dag.ready()] == ["t2"]
    with pytest.raises(TaskDAGError):
        dag.transition("t1", TaskStatus.RUNNING)


def test_dag_snapshot_rejects_unknown_dependency():
    from src.collaboration.dag import TaskDAG, TaskDAGError

    with pytest.raises(TaskDAGError, match="unknown dependencies"):
        TaskDAG.from_snapshot(
            {"tasks": {"t1": {"task_id": "t1", "role": "x", "kind": "x", "depends_on": ["missing"]}}}
        )


def test_collaboration_store_lease_retry_and_events(tmp_path):
    from src.collaboration.contracts import AgentResult, AgentTask, TaskStatus
    from src.collaboration.store import CollaborationStore, LeaseConflictError

    store = CollaborationStore(str(tmp_path))
    task = store.create_task(
        AgentTask(task_id="t1", run_id="r1", role="patcher", kind="patch", max_attempts=2)
    )
    store.claim_task(task.task_id, "worker-a", lease_seconds=10)
    with pytest.raises(LeaseConflictError):
        store.claim_task(task.task_id, "worker-b", lease_seconds=10)
    retried = store.complete_task(
        task.task_id,
        AgentResult(task.task_id, status=TaskStatus.FAILED, error="transient"),
        worker="worker-a",
    )
    assert retried.status == TaskStatus.READY
    assert retried.retry_at > time.time()
    assert any(event["event_type"] == "task_completed" for event in store.events(run_id="r1"))


def test_collaboration_store_rejects_stale_lease_update(tmp_path):
    from src.collaboration.contracts import AgentResult, AgentTask
    from src.collaboration.store import CollaborationStore, LeaseConflictError

    store = CollaborationStore(str(tmp_path))
    task = store.create_task(AgentTask(task_id="stale", run_id="r1", role="patcher", kind="patch"))
    store.claim_task(task.task_id, "worker-a", lease_seconds=0.1)
    time.sleep(0.15)
    store.claim_task(task.task_id, "worker-b", lease_seconds=10)
    with pytest.raises(LeaseConflictError):
        store.complete_task(
            task.task_id,
            AgentResult(task.task_id),
            worker="worker-a",
        )


def test_budget_backpressure_and_idempotent_reservation():
    from src.collaboration.budget import BudgetLedger, BudgetLimits

    ledger = BudgetLedger(BudgetLimits(tokens=10, concurrency=1))
    first = ledger.reserve(role="patcher", costs={"tokens": 8}, reservation_id="r1")
    assert first.allowed
    assert not ledger.reserve(role="verifier", costs={"tokens": 1}, reservation_id="r2").allowed
    assert (
        ledger.reserve(role="patcher", costs={"tokens": 8}, reservation_id="r1").reason
        == "idempotent"
    )
    ledger.release("r1")
    assert ledger.reserve(role="verifier", costs={"tokens": 1}, reservation_id="r2").allowed


def test_effect_receipt_is_idempotent_and_requires_revalidation():
    from src.collaboration.effects import EffectLedger, EffectReceipt, EffectStatus

    ledger = EffectLedger()
    receipt = EffectReceipt("e1", "apply_patch", "idem-1")
    assert ledger.prepare(receipt) is receipt
    assert ledger.prepare(EffectReceipt("e2", "apply_patch", "idem-1")) is receipt
    ledger.transition("idem-1", EffectStatus.UNCERTAIN)
    ledger.transition("idem-1", EffectStatus.REVALIDATE)
    assert ledger.transition("idem-1", EffectStatus.COMMITTED).status == EffectStatus.COMMITTED


def test_effect_receipt_persists_and_rejects_conflicting_idempotency_key(tmp_path):
    from src.collaboration.effects import EffectLedger, EffectReceipt, EffectStatus
    from src.collaboration.store import CollaborationStore

    store = CollaborationStore(str(tmp_path))
    ledger = EffectLedger(store)
    ledger.prepare(EffectReceipt("e1", "apply_patch", "idem-1", payload_hash="hash-a"))
    ledger.transition("idem-1", EffectStatus.COMMITTED)
    restored = EffectLedger(store)
    assert restored.get("idem-1").status == EffectStatus.COMMITTED
    with pytest.raises(ValueError):
        restored.prepare(EffectReceipt("e2", "run_tool", "idem-1", payload_hash="hash-b"))


def test_blackboard_event_replay_and_state_roundtrip():
    from src.blackboard import Blackboard
    from src.state import RepairState

    board = Blackboard()
    assert board.write("context:x", {"value": 1}, "localizer", evidence_refs=["OBS-1"])
    replayed = Blackboard.replay_events(board.event_log())
    assert replayed.read("context:x") == {"value": 1}
    state = RepairState(issue_input="fix", collaboration_tasks=[{"task_id": "t1"}])
    restored = RepairState.from_dict(state.to_dict())
    assert restored.collaboration_tasks == [{"task_id": "t1"}]


def test_handoff_requires_active_roles_and_isolation_projection():
    from src.collaboration.contracts import Handoff
    from src.collaboration.isolation import role_projection, validate_independent_input
    from src.collaboration_governance import CollaborationGovernance
    from src.state import RepairState

    state = RepairState(issue_input="fix", active_roles=["patcher", "critic"])
    handoff = Handoff(
        task_id="t1",
        from_role="patcher",
        to_role="critic",
        output_schema="CandidatePatch[]",
    )
    payload = CollaborationGovernance.record_handoff(state, handoff)
    assert payload["status"] == "accepted"
    projection = role_projection(state, "critic")
    assert not validate_independent_input(projection, expected_role="critic")
    assert "action_ledger" not in projection


def test_repair_state_validates_collaboration_graph():
    from src.state import RepairState

    state = RepairState(
        issue_input="fix",
        collaboration_tasks=[
            {"task_id": "t1", "depends_on": []},
            {"task_id": "t1", "depends_on": ["missing"]},
        ],
    )
    errors = state.validate_invariants()
    assert "collaboration task_ids must be unique" in errors
    assert "collaboration task has unknown dependency" in errors


def test_critic_verdict_is_structured_and_independent():
    from src.repair.critic import review_patch
    from src.state import CandidatePatch

    verdict = review_patch([CandidatePatch(file_path="src/app.py", diff="+x")])
    assert verdict.independent is True
    assert verdict.verdict_id.startswith("critic-")
    assert verdict.to_dict()["coverage_status"] == "not_evaluated"
