"""Run owner fencing and cancellation protocol tests."""

from __future__ import annotations

import multiprocessing
import os
import time
from dataclasses import dataclass

import pytest

from agent_runtime.run_coordination import (
    CoordinationError,
    CoordinationIntegrityError,
    OwnerConflictError,
    ResourceResult,
    RunCoordinationStore,
    RunCoordinator,
    StaleGenerationError,
)


def _acquire(path: str, state_root: str, output) -> None:
    try:
        RunCoordinationStore(path, state_root=state_root).acquire("task", "run")
    except Exception as exc:  # process-safe assertion payload
        output.put(type(exc).__name__)
    else:
        output.put("acquired")


def test_same_run_has_one_cross_process_owner(tmp_path):
    workspace = str(tmp_path / "workspace")
    state_root = str(tmp_path / "state")
    (tmp_path / "workspace").mkdir()
    first = RunCoordinationStore(workspace, state_root=state_root).acquire("task", "run")
    queue = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_acquire, args=(workspace, state_root, queue))
    proc.start()
    proc.join(10)
    assert proc.exitcode == 0
    assert queue.get(timeout=2) == "OwnerConflictError"
    assert first.generation == 1


def test_stale_generation_is_fenced_after_takeover(tmp_path):
    workspace = str(tmp_path / "workspace")
    state_root = str(tmp_path / "state")
    (tmp_path / "workspace").mkdir()
    store = RunCoordinationStore(workspace, state_root=state_root)
    old = store.acquire("task", "run", lease_seconds=0.1)
    import time

    time.sleep(0.15)
    new = store.acquire("task", "run")
    assert new.generation == old.generation + 1
    with pytest.raises(StaleGenerationError):
        store.assert_lease(old)


@dataclass
class Adapter:
    calls: list[str]
    result: ResourceResult

    def cancel(self, resource, request_id):
        self.calls.append(resource.resource_id)
        return self.result

    def reconcile(self, resource):
        return ResourceResult(resource.resource_id, "not_started", cleanup="confirmed")


def test_cancel_cleans_children_before_parent_and_is_idempotent(tmp_path):
    workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    adapter = Adapter([], ResourceResult("", "cancelled", cleanup="confirmed"))
    coordinator = RunCoordinator(workspace, "task", "run", adapters={"*": adapter})
    coordinator.acquire()
    coordinator.reconcile()
    parent = coordinator.register_resource(resource_id="plan", kind="plan_attempt", effect="write")
    coordinator.register_resource(
        resource_id="sandbox", kind="sandbox_call", effect="write", parent_id=parent.resource_id
    )
    report = coordinator.cancel("cancel-1")
    assert report.confirmed
    assert adapter.calls == ["sandbox", "plan"]
    again = coordinator.cancel("cancel-1")
    assert again.status == "cancelled"
    assert adapter.calls == ["sandbox", "plan"]


def test_unknown_cleanup_stays_recovery_required(tmp_path):
    workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    adapter = Adapter(
        [], ResourceResult("", "unknown", cleanup="unknown", error_code="process_left")
    )
    coordinator = RunCoordinator(workspace, "task", "run", adapters={"*": adapter})
    coordinator.acquire()
    coordinator.reconcile()
    coordinator.register_resource(resource_id="sandbox", kind="sandbox_call", effect="write")
    report = coordinator.cancel("cancel-1")
    assert report.status == "recovery_required"
    assert coordinator.store.snapshot("run").status == "recovery_required"


def _race_owner(workspace, ready, start, release, output):
    store = RunCoordinationStore(workspace)
    ready.put(True)
    start.wait(10)
    try:
        store.acquire("task", "run")
        output.put("acquired")
        release.wait(10)
    except OwnerConflictError:
        output.put("conflict")


def test_simultaneous_resumes_admit_exactly_one_executor(tmp_path):
    ready, output = multiprocessing.Queue(), multiprocessing.Queue()
    start, release = multiprocessing.Event(), multiprocessing.Event()
    workers = [
        multiprocessing.Process(
            target=_race_owner, args=(str(tmp_path), ready, start, release, output)
        )
        for _ in range(3)
    ]
    try:
        for proc in workers:
            proc.start()
        for _ in workers:
            ready.get(timeout=10)
        start.set()
        results = [output.get(timeout=10) for _ in workers]
        assert results.count("acquired") == 1
        assert results.count("conflict") == 2
    finally:
        release.set()
        for proc in workers:
            proc.join(10)
            if proc.is_alive():
                proc.terminate()
                proc.join(5)


def _crash_owner(workspace):
    RunCoordinationStore(workspace).acquire("task", "run", lease_seconds=60)
    os._exit(71)


def test_dead_owner_can_be_replaced_before_lease_expiry(tmp_path):
    proc = multiprocessing.Process(target=_crash_owner, args=(str(tmp_path),))
    proc.start()
    proc.join(10)
    assert proc.exitcode == 71
    lease = RunCoordinationStore(str(tmp_path)).acquire("task", "run")
    assert lease.generation == 2


def test_unknown_process_identity_does_not_authorize_takeover(tmp_path):
    store = RunCoordinationStore(str(tmp_path))
    store.acquire("task", "run", owner_identity={"pid": os.getpid(), "generation": "unknown"})
    with pytest.raises(OwnerConflictError):
        store.acquire("task", "run")


def _active(tmp_path, adapters=None):
    coordinator = RunCoordinator(str(tmp_path), "task", "run", adapters=adapters)
    coordinator.acquire()
    coordinator.reconcile()
    return coordinator


def test_cancel_closes_every_dispatch_boundary(tmp_path):
    coordinator = _active(tmp_path)
    resource = coordinator.register_resource(resource_id="pending", kind="tool", effect="write")
    coordinator.request_cancel("cancel")
    dispatched = []
    with pytest.raises(StaleGenerationError):
        coordinator.dispatch(resource, lambda: dispatched.append(True))
    with pytest.raises(CoordinationError, match="dispatch_closed"):
        coordinator.store.register_resource(
            coordinator.lease, resource_id="late", kind="tool", effect="read"
        )
    with pytest.raises(CoordinationError, match="dispatch_closed"):
        coordinator.transition_resource(resource.resource_id, "running")
    assert dispatched == []


def test_cancel_callback_closes_durable_gate_immediately(tmp_path):
    from agent_runtime.cancellation import CancellationToken

    coordinator = _active(tmp_path)
    token = CancellationToken()
    remove = token.add_callback(coordinator.request_cancel)
    token.cancel("user")
    assert coordinator.store.snapshot("run").status == "cancel_requested"
    remove()
    token.cancel("again")


def test_three_level_dag_cleanup_continues_after_adapter_failure(tmp_path):
    calls = []

    class FailingAdapter:
        def cancel(self, resource, request_id):
            calls.append(resource.resource_id)
            if resource.resource_id == "command":
                raise OSError("lost transport")
            return ResourceResult(resource.resource_id, "cancelled", cleanup="confirmed")

    coordinator = _active(tmp_path, {"*": FailingAdapter()})
    coordinator.register_resource(resource_id="agent", kind="agent_task", effect="write")
    coordinator.register_resource(
        resource_id="attempt", kind="plan_attempt", effect="write", parent_id="agent"
    )
    coordinator.register_resource(
        resource_id="command", kind="sandbox_call", effect="write", parent_id="attempt"
    )
    report = coordinator.cancel()
    assert calls == ["command", "attempt", "agent"]
    assert report.status == "recovery_required"
    assert report.resources[0].error_code == "resource_cleanup_exception"
    with pytest.raises(CoordinationError, match="workspace_busy"):
        coordinator.store.acquire("other", "other")


def test_cancel_recovery_finishes_existing_request_without_reopening_dispatch(tmp_path):
    adapter = Adapter([], ResourceResult("", "unknown", cleanup="unknown"))
    old = _active(tmp_path, {"*": adapter})
    old.register_resource(resource_id="leftover", kind="sandbox_call", effect="write")
    assert old.cancel("original").status == "recovery_required"
    new = RunCoordinator(str(tmp_path), "task", "run", adapters={"*": adapter})
    new.acquire()
    report = new.reconcile()
    assert report["status"] == "cancelled"
    assert new.store.snapshot("run").cancel_request_id == "original"
    with pytest.raises(StaleGenerationError):
        new.assert_can_dispatch()


def test_checkpoint_history_is_verified_and_tampering_rejected(tmp_path):
    coordinator = _active(tmp_path)
    seal = coordinator.store.checkpoint_seal(coordinator.lease)
    coordinator.finish("released")
    replacement = RunCoordinator(str(tmp_path), "task", "run")
    replacement.acquire()
    replacement.store.verify_checkpoint_seal(replacement.lease, seal)
    for key, value in [
        ("owner_token", "forged"),
        ("coordination_revision", 999),
        ("resource_ref_checksum", "bad"),
    ]:
        with pytest.raises(CoordinationIntegrityError):
            replacement.store.verify_checkpoint_seal(replacement.lease, {**seal, key: value})


def test_expired_owner_cannot_renew_or_register(tmp_path):
    coordinator = _active(tmp_path)
    lease = coordinator.store.heartbeat(coordinator.lease, lease_seconds=0.1)
    time.sleep(0.15)
    with pytest.raises(StaleGenerationError):
        coordinator.store.heartbeat(lease)
    with pytest.raises(StaleGenerationError):
        coordinator.store.register_resource(
            lease, resource_id="late", kind="command", effect="write"
        )


def test_finish_with_residual_resource_remains_recoverable(tmp_path):
    coordinator = _active(tmp_path)
    coordinator.register_resource(resource_id="test-process", kind="sandbox_call", effect="write")
    assert coordinator.finish("released").status == "recovery_required"


def test_phase_tasks_publish_terminal_cleanup(tmp_path):
    from src.collaboration.repair_runtime import RepairCollaborationRuntime
    from src.state import RepairState

    state = RepairState(issue_input="fix", repair_run_id="run")
    runtime = RepairCollaborationRuntime(str(tmp_path), "run", state)
    coordinator = _active(tmp_path)
    runtime.attach_coordinator(coordinator)
    for phase in ("context", "patch", "verify"):
        runtime.advance(phase, state)
    runtime.advance("done", state, terminal_status="fixed")
    assert coordinator.finish("released").status == "released"
    assert all(
        r.status == "completed" and r.cleanup == "confirmed"
        for r in coordinator.store.resources("run")
    )


def test_unknown_write_receipt_never_becomes_completed(tmp_path):
    from types import SimpleNamespace

    from agent_runtime.run_coordination import ResourceRecord
    from agent_runtime.run_coordination.adapters import PlanAttemptAdapter

    attempt = {"phase": "dispatched", "owner": {"pid": 99999999, "generation": 1}}
    session = SimpleNamespace(
        store=SimpleNamespace(latest=lambda *args: {"write": attempt}),
        operations=lambda _: [{"phase": "dispatched", "effect": "write"}],
    )
    result = PlanAttemptAdapter(session).reconcile(
        ResourceRecord("write", "task", "run", "ws", effect="write")
    )
    assert not result.confirmed and result.status == "unknown"


def test_cancel_during_tool_preparation_blocks_actual_dispatch(tmp_path, monkeypatch):
    import threading

    from agent_runtime.cancellation import CancellationToken
    from agent_runtime.tool_executor import ToolExecutor
    from tests.plan_l2_support import repair_fixture

    orch, _, _ = repair_fixture(tmp_path)
    agent = orch.patcher
    coordinator = _active(tmp_path)
    token = CancellationToken()
    agent.cancel_token = token
    agent.tool_context.run_coordinator = coordinator
    coordinator.cancel_token = token
    remove = token.add_callback(coordinator.request_cancel)
    executor = ToolExecutor(agent=agent, approval_policy="auto")
    prepared, release = threading.Event(), threading.Event()
    calls, outcomes = [], []

    def prepare():
        prepared.set()
        assert release.wait(3)
        return {}

    executor._high_risk_tools = executor._high_risk_tools | {"list_files"}
    monkeypatch.setattr(executor, "_capture_snapshot", prepare)
    monkeypatch.setattr(executor, "_capture_restore_snapshot", lambda: {})
    monkeypatch.setattr(executor, "_run_tool", lambda *args: calls.append(True))
    worker = threading.Thread(
        target=lambda: outcomes.append(executor.execute_gated("list_files", {"path": "."}))
    )
    try:
        worker.start()
        assert prepared.wait(3)
        token.cancel()
        assert coordinator.store.snapshot("run").status == "cancel_requested"
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert not calls
        assert outcomes[0].metadata["tool_status"] == "rejected"
        assert outcomes[0].metadata["rejection_layer"] == "cancel"
    finally:
        release.set()
        worker.join(3)
        remove()


def test_failed_immediate_cancel_subscription_is_removed():
    from agent_runtime.cancellation import CancellationToken

    token = CancellationToken()
    token.cancel()
    calls = []

    def fail():
        calls.append(True)
        raise OSError("durable_cancel_failure")

    with pytest.raises(OSError, match="durable_cancel_failure"):
        token.add_callback(fail)
    token.cancel()
    assert calls == [True]


def test_cancel_notifies_other_gates_after_one_callback_fails():
    from agent_runtime.cancellation import CancellationToken

    token = CancellationToken()
    calls = []

    def fail():
        raise OSError("durable_cancel_failure")

    token.add_callback(fail)
    token.add_callback(lambda: calls.append(True))
    with pytest.raises(OSError, match="durable_cancel_failure"):
        token.cancel()
    assert calls == [True]
