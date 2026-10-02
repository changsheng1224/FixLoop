"""Owner lifecycle and public repair boundaries; no model or subprocess work."""

import threading
import time
from types import SimpleNamespace

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.run_coordination import OwnerConflictError, RunCoordinationStore, RunCoordinator
from src.repair.plan_binding import RepairPlanBinding
from src.repair.verification.repair_timeout import handle_repair_wall_timeout
from tests.plan_l2_support import repair_fixture


def test_early_close_relinquishes_owner_and_is_idempotent(tmp_path):
    orch, state, _ = repair_fixture(tmp_path)
    binding = RepairPlanBinding(orch, state, defer_plan=True)
    binding.close()
    binding.close()
    snapshot = binding.coordinator.store.snapshot(state.repair_run_id)
    assert snapshot.status == "released" and not snapshot.owner_token
    assert orch.patcher.tool_context.run_coordinator is None
    replacement = RepairPlanBinding(orch, state, defer_plan=True)
    try:
        assert replacement.coordinator.lease.generation == 2
    finally:
        replacement.close()


def test_early_close_preserves_unknown_resources(tmp_path):
    orch, state, _ = repair_fixture(tmp_path)
    binding = RepairPlanBinding(orch, state, defer_plan=True)
    binding.coordinator.register_resource(resource_id="leftover", kind="command", effect="write")
    binding.close()
    assert binding.coordinator.store.snapshot(state.repair_run_id).status == "recovery_required"
    assert state.control.coordination_status == "recovery_required"


def test_session_open_failure_releases_fence_and_cancel_subscription(tmp_path, monkeypatch):
    import src.repair.plan_binding as module

    orch, state, _ = repair_fixture(tmp_path)
    token = CancellationToken()
    orch.patcher.cancel_token = token

    def fail_open(*args, **kwargs):
        raise ValueError("injected_journal_failure")

    monkeypatch.setattr(module, "PlanSession", fail_open)
    with pytest.raises(ValueError, match="injected_journal_failure"):
        RepairPlanBinding(orch, state, defer_plan=True)
    snapshot = RunCoordinationStore(str(tmp_path)).snapshot(state.repair_run_id)
    assert snapshot.status == "recovery_required" and not snapshot.owner_token
    assert orch.patcher.tool_context.run_coordinator is None
    token.cancel()  # no callback referring to a closed owner may survive


def test_verified_recovery_clears_old_context_dispatch_block(tmp_path):
    orch, state, _ = repair_fixture(tmp_path)
    old = RunCoordinator(str(tmp_path), state.repair_run_id, state.repair_run_id)
    old.acquire()
    old.reconcile()
    old.register_resource(resource_id="confirmed", kind="command", effect="write")
    old.transition_resource("confirmed", "completed", cleanup="confirmed")
    old.finish("released")
    orch.patcher.tool_context.execution_uncertain = True
    binding = RepairPlanBinding(orch, state, defer_plan=True)
    try:
        assert not orch.patcher.tool_context.execution_uncertain
    finally:
        binding.close()


def test_public_resume_rejects_loser_before_task_or_trace_mutation(tmp_path):
    first, state, _ = repair_fixture(tmp_path)
    first._begin_repair_trace(state)
    second, other_state, _ = repair_fixture(tmp_path, resume=True)
    before = first._collaboration_runtime.store.list_tasks(state.repair_run_id)
    versions = {task.task_id: task.version for task in before}
    trace_store = first._repair_ctx.repair_tracer.store
    trace_events = trace_store.load_trace_events(state.repair_run_id)
    assert trace_events
    try:
        with pytest.raises(OwnerConflictError):
            second._begin_repair_trace(other_state)
        after = first._collaboration_runtime.store.list_tasks(state.repair_run_id)
        assert {task.task_id: task.version for task in after} == versions
        assert second._collaboration_runtime is None
        assert trace_store.load_trace_events(state.repair_run_id) == trace_events
    finally:
        first._end_repair_trace(state)
        first._plan_binding.close()


def test_live_worker_timeout_preserves_disk_and_recovery_state(tmp_path):
    orch, state, _ = repair_fixture(tmp_path)
    binding = RepairPlanBinding(orch, state, defer_plan=True)
    orch._plan_binding = binding
    binding.coordinator.register_resource(
        resource_id="live-worker", kind="agent_task", effect="write"
    )
    finished = []
    orch._collaboration_runtime = SimpleNamespace(finish_cancelled=lambda _: finished.append(True))
    target = tmp_path / "value.py"
    before = orch._snapshot_repo()
    target.write_text("unconfirmed write\n")
    try:
        handle_repair_wall_timeout(
            orch,
            state,
            initial_snapshot=before,
            repair_timeout_s=1,
            cancel_token=CancellationToken(),
            grace_s=0,
        )
        assert state.status == "recovery_required"
        assert target.read_text() == "unconfirmed write\n"
        assert finished == []
        assert binding.coordinator.store.snapshot(state.repair_run_id).status == "recovery_required"
    finally:
        binding.close()


def test_trace_initialization_failure_relinquishes_entry_owner(tmp_path, monkeypatch):
    from src.repair.run_trace import RepairRunTracer

    orch, state, _ = repair_fixture(tmp_path)

    def fail_begin(*args, **kwargs):
        raise OSError("injected_trace_failure")

    monkeypatch.setattr(RepairRunTracer, "begin", fail_begin)
    with pytest.raises(OSError, match="injected_trace_failure"):
        orch._begin_repair_trace(state)
    snapshot = RunCoordinationStore(str(tmp_path)).snapshot(state.repair_run_id)
    assert snapshot.status == "recovery_required" and not snapshot.owner_token


def test_public_resume_finishes_persisted_cancel_without_model_calls(tmp_path):
    from src.repair.checkpoint_load import save_repair_checkpoint

    orch, state, client = repair_fixture(tmp_path, resume=True, full=True)
    save_repair_checkpoint(state, str(tmp_path))
    old = RunCoordinator(str(tmp_path), state.repair_run_id, state.repair_run_id)
    old.acquire()
    old.reconcile()
    old.register_resource(resource_id="prepared", kind="plan_attempt", effect="read")
    assert old.cancel("persisted").status == "recovery_required"
    token = CancellationToken()
    token.cancel()
    result = orch.repair(
        state.issue_input, resume_run_id=state.repair_run_id, cancel_token=token, repair_timeout_s=0
    )
    assert result.status == "user_cancel"
    assert result.control.coordination_status == "cancelled"
    assert client.session_usage["calls"] == 0
    assert orch.patcher.tool_context.run_coordinator is None


def test_finalize_preserves_worktree_when_resource_cleanup_is_unknown(tmp_path, monkeypatch):
    orch, state, _ = repair_fixture(tmp_path)
    binding = RepairPlanBinding(orch, state, defer_plan=True)
    orch._plan_binding = binding
    binding.coordinator.register_resource(resource_id="live", kind="command", effect="write")
    removed = []
    monkeypatch.setattr(orch, "_maybe_leave_worktree", lambda **_: removed.append(True))
    try:
        orch._end_repair_trace(state)
        assert removed == []
        assert state.status == "recovery_required"
    finally:
        binding.close()


def test_binding_heartbeat_renews_owner_and_stale_close_preserves_replacement(tmp_path):
    orch, state, _ = repair_fixture(tmp_path)
    entry = RunCoordinator(
        str(tmp_path), state.repair_run_id, state.repair_run_id, lease_seconds=0.6
    )
    entry.acquire()
    orch._entry_coordinator = entry
    binding = RepairPlanBinding(orch, state, defer_plan=True)
    initial_expiry = binding.coordinator.lease.lease_expires_at
    deadline = time.monotonic() + 2
    try:
        while binding.coordinator.lease.lease_expires_at <= initial_expiry:
            assert time.monotonic() < deadline, "heartbeat did not renew"
            threading.Event().wait(0.02)
        binding.coordinator.assert_can_dispatch()
        binding.coordinator.finish("released")
        replacement = RunCoordinator(str(tmp_path), state.repair_run_id, state.repair_run_id)
        replacement.acquire()
        replacement.reconcile()
        orch.patcher.tool_context.run_coordinator = replacement
        binding.close()
        replacement.assert_can_dispatch()
        assert orch.patcher.tool_context.run_coordinator is replacement
        replacement.finish("released")
    finally:
        binding.close()


def test_public_resume_returns_recovery_state_for_unknown_resource(tmp_path):
    from src.repair.checkpoint_load import save_repair_checkpoint

    orch, state, client = repair_fixture(tmp_path, resume=True, full=True)
    save_repair_checkpoint(state, str(tmp_path))
    old = RunCoordinator(str(tmp_path), state.repair_run_id, state.repair_run_id)
    old.acquire()
    old.reconcile()
    old.register_resource(resource_id="untracked-worker", kind="command", effect="write")
    old.finish("recovery_required")
    result = orch.repair(state.issue_input, resume_run_id=state.repair_run_id, repair_timeout_s=0)
    assert result.status == "recovery_required"
    assert client.session_usage["calls"] == 0
    assert (
        RunCoordinationStore(str(tmp_path)).snapshot(state.repair_run_id).status
        == "recovery_required"
    )


def test_long_task_initialization_failure_releases_plan_lease(tmp_path, monkeypatch):
    from agent_runtime.plan_runtime.long_task import LongTaskState
    from agent_runtime.plan_runtime.session import PlanSession

    orch, state, _ = repair_fixture(tmp_path)
    with PlanSession(
        str(tmp_path), state.repair_run_id, state.repair_run_id, orch.patcher.tools
    ) as session:
        session.configure_long_task(state.issue_input)

    def fail_verify(*args):
        raise ValueError("injected_long_task_failure")

    with monkeypatch.context() as patch:
        patch.setattr(LongTaskState, "verify", fail_verify)
        with pytest.raises(ValueError, match="injected_long_task_failure") as failure:
            PlanSession(str(tmp_path), state.repair_run_id, state.repair_run_id, orch.patcher.tools)
    with PlanSession(
        str(tmp_path), state.repair_run_id, state.repair_run_id, orch.patcher.tools
    ) as session:
        assert session.long_task_state.original_request == state.issue_input
    assert str(failure.value) == "injected_long_task_failure"


def test_confirmed_cancel_rolls_back_before_releasing_owner(tmp_path):
    orch, state, _ = repair_fixture(tmp_path)
    token = CancellationToken()
    orch.patcher.cancel_token = token
    orch._repair_ctx.cancel_token = token
    before = orch._snapshot_repo()
    patches, _ = orch._run_patcher_toolized(state, "fix answer", {})
    assert patches
    binding = orch._plan_binding
    try:
        token.cancel()
        assert orch._cancel_run_resources(state)
        orch._restore_repo_snapshot(before)
        assert (tmp_path / "value.py").read_text() == before["value.py"]
        orch._end_repair_trace(state)
        snapshot = binding.coordinator.store.snapshot(state.repair_run_id)
        assert snapshot.status == "cancelled" and not snapshot.owner_token
    finally:
        binding.close()


def test_unknown_patcher_write_is_not_reverted_by_export_gate(tmp_path, monkeypatch):
    orch, state, _ = repair_fixture(tmp_path)
    target = tmp_path / "unrelated.py"
    target.write_text("before\n")

    def uncertain_write(*args, **kwargs):
        target.write_text("unconfirmed write\n")
        orch.patcher.tool_context.execution_uncertain = True
        return "unknown write result", {"total_ms": 1}

    monkeypatch.setattr(orch, "_run_agent", uncertain_write)
    patches, timing = orch._run_patcher_agent(state, "repair", {})
    assert target.read_text() == "unconfirmed write\n"
    assert not patches
    assert timing["execution_uncertain"] is True
    assert state.status == "recovery_required"


def test_public_cancel_after_patch_completes_cleanup_and_rollback(tmp_path, monkeypatch):
    orch, seed, client = repair_fixture(tmp_path, full=True)
    token = CancellationToken()
    complete = client.complete

    def cancel_after_final(*args, **kwargs):
        output = complete(*args, **kwargs)
        if output.startswith("<final>"):
            token.cancel()
        return output

    monkeypatch.setattr(client, "complete", cancel_after_final)
    before = (tmp_path / "value.py").read_text()
    result = orch.repair(
        seed.issue_input, cancel_token=token, repair_timeout_s=0, run_id=seed.repair_run_id
    )
    assert result.status == "user_cancel", (result.agent_errors, result.node_timings)
    assert (tmp_path / "value.py").read_text() == before
    snapshot = RunCoordinationStore(str(tmp_path)).snapshot(result.repair_run_id)
    assert snapshot.status == "cancelled" and not snapshot.owner_token
    assert all(r.cleanup == "confirmed" for r in snapshot.resources)
