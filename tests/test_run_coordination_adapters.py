"""Recovery must use receipts from the exact execution, including cancel races."""

from types import SimpleNamespace

import pytest

from agent_runtime.run_coordination import ResourceRecord
from agent_runtime.run_coordination.adapters import SandboxCallAdapter


@pytest.mark.parametrize("mismatch", ["owner_token", "generation", "call_id"])
def test_cancel_rejects_receipt_from_other_execution(mismatch):
    current = {"call_id": "call", "state": "running", "owner_token": "owner", "generation": 1}
    cancelled = []

    def cancel(call_id):
        cancelled.append(call_id)
        current.update(
            state="terminal", result={"execution_status": "cancelled", "cleanup": "confirmed"}
        )
        current[mismatch] = "other" if mismatch != "generation" else 2

    backend = SimpleNamespace(inspect_receipt=lambda _: dict(current), cancel=cancel)
    resource = ResourceRecord(
        "call",
        "task",
        "run",
        "ws",
        kind="sandbox_call",
        payload={"owner_token": "owner", "generation": 1},
    )
    result = SandboxCallAdapter(backend).cancel(resource, "request")
    assert cancelled == ["call"]
    assert not result.confirmed
    assert result.error_code in {
        "sandbox_receipt_owner_mismatch",
        "sandbox_receipt_identity_mismatch",
    }


@pytest.mark.parametrize("no_target_started", [True, False])
def test_start_failure_requires_durable_no_target_evidence(no_target_started):
    receipt = {
        "call_id": "call",
        "state": "terminal",
        "no_target_started": no_target_started,
        "result": {"execution_status": "start_failed", "cleanup": "unverified"},
    }
    backend = SimpleNamespace(inspect_receipt=lambda _: receipt)
    resource = ResourceRecord("call", "task", "run", "ws", kind="sandbox_call")
    result = SandboxCallAdapter(backend).reconcile(resource)
    assert result.confirmed is no_target_started


@pytest.mark.parametrize("task_status", ["pending", "running", "expired"])
def test_subagent_cancel_requires_terminal_worker_evidence(tmp_path, task_status):
    from agent_runtime.run_coordination.adapters import CollaborationTaskAdapter
    from src.collaboration.contracts import AgentTask, TaskStatus
    from src.collaboration.store import CollaborationStore, LeaseConflictError

    store = CollaborationStore(str(tmp_path))
    store.create_task(
        AgentTask(
            task_id="agent",
            run_id="run",
            role="patcher",
            kind="patch",
            status=TaskStatus(task_status),
        )
    )
    resource = ResourceRecord("agent", "task", "run", "ws", kind="agent_task")
    result = CollaborationTaskAdapter(store, "run").cancel(resource, "cancel")
    assert result.confirmed is (task_status == "pending")
    with pytest.raises(LeaseConflictError):
        store.claim_task("agent", "late-worker")
    if task_status != "pending":
        assert result.error_code == "agent_task_process_unconfirmed"


def test_phase_runtime_does_not_confirm_expired_external_subagent(tmp_path):
    from agent_runtime.run_coordination import RunCoordinator
    from src.collaboration.contracts import AgentTask, TaskStatus
    from src.collaboration.repair_runtime import RepairCollaborationRuntime
    from src.state import RepairState

    state = RepairState(issue_input="repair", repair_run_id="run")
    runtime = RepairCollaborationRuntime(str(tmp_path), "run", state)
    runtime.store.create_task(
        AgentTask(
            task_id="external",
            run_id="run",
            role="patcher",
            kind="subagent",
            status=TaskStatus.EXPIRED,
        )
    )
    coordinator = RunCoordinator(str(tmp_path), "run", "run")
    coordinator.acquire()
    coordinator.reconcile()
    runtime.attach_coordinator(coordinator)
    resource = next(
        r for r in coordinator.store.resources("run") if r.resource_id == "agent:external"
    )
    assert resource.cleanup != "confirmed"
    assert resource.payload["execution_mode"] == "subagent"


def test_subagent_claim_race_cannot_drop_cancel_request(tmp_path, monkeypatch):
    from src.collaboration.contracts import AgentTask, TaskStatus
    from src.collaboration.store import CollaborationStore, LeaseConflictError

    store = CollaborationStore(str(tmp_path))
    store.create_task(AgentTask(task_id="agent", run_id="run", role="patcher", kind="subagent"))
    get_task = store.get_task
    claimed = []

    def read_with_concurrent_claim(task_id):
        task = get_task(task_id)
        if not claimed and task.status == TaskStatus.PENDING:
            store.claim_task(task_id, "racing-worker")
            claimed.append(True)
        return task

    monkeypatch.setattr(store, "get_task", read_with_concurrent_claim)
    store.request_cancel_task("agent", "cancel")
    task = get_task("agent")
    assert task.payload["cancel_request_id"] == "cancel"
    assert task.status in {TaskStatus.RUNNING, TaskStatus.CANCELLED}
    with pytest.raises(LeaseConflictError):
        store.claim_task("agent", "late-worker")
