"""Fresh and resumed repairs must enforce the same lifecycle and policy gates."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.tool_context import ToolContext
from src.orchestrator import Orchestrator
from src.repair.checkpoint_load import save_repair_checkpoint
from src.repair.execution.edit_lock import (
    EditLockState,
)
from src.repair.phase_clock import PhaseTimeoutConfig
from src.repair.progress import ProgressEmitter
from src.state import CandidatePatch, RepairPlan, RepairState, VerificationResult


@pytest.fixture(params=[False, True], ids=["fresh", "resume"])
def flow(request, tmp_path, monkeypatch):
    """Exercise the public API with deterministic patch/verify boundaries."""
    orch = Orchestrator(None, use_pytest_verify=False)
    orch._repo_root = str(tmp_path)
    events = []
    orch._progress = ProgressEmitter(quiet=True, record=events.append)
    monkeypatch.setattr(orch, "_begin_repair_trace", lambda _: None)
    monkeypatch.setattr(orch, "_parse_issue", lambda _: RepairPlan())
    monkeypatch.setattr(orch, "_snapshot_repo", lambda: {"value.py": "value = 1\n"})
    monkeypatch.setattr(orch, "_restore_repo_snapshot", Mock())
    monkeypatch.setattr("src.repair.pipeline._record_pytest_exit", lambda *a, **kw: None)
    monkeypatch.setattr(
        orch,
        "_run_patcher",
        lambda _: (
            [
                CandidatePatch(
                    file_path="value.py", original_lines="value = 1", patched_lines="value = 2"
                )
            ],
            {"total_ms": 1, "model_call_ms": 0, "parse_apply_ms": 1},
        ),
    )
    monkeypatch.setattr(orch, "_run_verifier", lambda _: VerificationResult(all_passed=True))
    resumed = request.param
    if resumed:
        saved = RepairState(issue_input="public issue", repair_run_id="parity", max_retries=1)
        saved.phase = "patch"
        save_repair_checkpoint(saved, str(tmp_path))
        monkeypatch.setattr(orch, "_parse_issue", Mock(side_effect=AssertionError("resume parsed")))

    def run(**kwargs):
        return orch.repair(
            "public issue",
            max_retries=1,
            repair_timeout_s=0,
            resume_run_id="parity" if resumed else "",
            **kwargs,
        )

    return orch, run, events


def test_pending_verify_completes_phase_and_progress(flow):
    orch, run, events = flow
    result = run()
    assert result.status == "pending_verify"
    assert result.phase == "done"
    names = [event.event for event in events]
    assert names.count("repair_started") == 1
    assert names.count("repair_finished") == 1
    assert "patcher_turn" in names
    assert orch._progress._hb_thread is None
    assert orch._edit_lock is None


def test_patch_budget_is_enforced(flow, monkeypatch):
    orch, run, _ = flow
    patcher = orch._run_patcher

    def slow_patch(state):
        patches, timing = patcher(state)
        timing["total_ms"] = 2000
        return patches, timing

    monkeypatch.setattr(orch, "_run_patcher", slow_patch)
    result = run(phase_timeouts=PhaseTimeoutConfig(0, 1, 0, 0))
    assert result.status == "timeout"
    assert result.control.phase_timeout == "patch"


def test_cancel_during_verifier_rolls_back(flow, monkeypatch):
    orch, run, _ = flow
    token = CancellationToken()
    monkeypatch.setattr(orch, "_verification_enabled", lambda: True)
    record_verify = Mock()
    monkeypatch.setattr(orch, "_record_l2_synthetic_ask", record_verify)

    def verify(state):
        token.cancel("user")
        return VerificationResult(all_passed=True)

    monkeypatch.setattr(orch, "_run_verifier", verify)
    result = run(cancel_token=token)
    assert result.status == "user_cancel"
    orch._restore_repo_snapshot.assert_called_once()
    record_verify.assert_not_called()


def test_failure_evidence_is_current_before_feedback(flow, monkeypatch):
    from src.repair.failure_ledger import load_ledger_from_state

    orch, run, _ = flow
    monkeypatch.setattr(orch, "_verification_enabled", lambda: True)
    monkeypatch.setattr(
        orch,
        "_run_verifier",
        lambda _: VerificationResult(
            all_passed=False,
            total_tests=1,
            failed=1,
            failure_logs=["AssertionError: value differs"],
        ),
    )
    seen = []

    def feedback(result, *, state):
        ledger = load_ledger_from_state(state)
        seen.append(ledger.to_dict())
        assert len(ledger.hypotheses) == 1
        assert ledger.hypotheses[0].counterexamples == ["AssertionError: value differs"]
        return "current evidence"

    monkeypatch.setattr(orch, "_build_feedback", feedback)
    run()
    assert len(seen) == 1


def test_signature_gate_precedes_verifier(flow, monkeypatch):
    orch, run, _ = flow
    monkeypatch.setattr(orch, "_verification_enabled", lambda: True)
    monkeypatch.setattr(
        orch,
        "_run_patcher",
        lambda _: (
            [
                CandidatePatch(
                    file_path="value.py",
                    original_lines="def value():\n    return 1",
                    patched_lines="def changed():\n    return 2",
                )
            ],
            {"total_ms": 1, "model_call_ms": 0, "parse_apply_ms": 1},
        ),
    )
    verify = Mock(side_effect=AssertionError("signature drift reached verifier"))
    monkeypatch.setattr(orch, "_run_verifier", verify)
    result = run()
    verify.assert_not_called()
    assert result.retry_count == 1
    assert "semantic_drift" in result.agent_errors


def test_success_resets_cooldown(flow, monkeypatch):
    orch, run, _ = flow
    monkeypatch.setattr(orch, "_verification_enabled", lambda: True)
    cooldown = Mock()
    orch._verify_cooldown = cooldown
    result = run()
    assert result.status == "fixed"
    cooldown.record_success.assert_called_once()


def test_partial_patch_timing_uses_elapsed_time(flow, monkeypatch):
    orch, run, _ = flow
    patcher = orch._run_patcher
    monkeypatch.setattr(orch, "_run_patcher", lambda state: (patcher(state)[0], {}))
    assert run().status == "pending_verify"


def test_startup_exception_stops_heartbeat(flow, monkeypatch):
    orch, run, _ = flow
    heartbeat = Mock(wraps=orch._progress.stop_heartbeat)
    monkeypatch.setattr(orch._progress, "stop_heartbeat", heartbeat)
    monkeypatch.setattr(orch, "_init_repair_blackboard", Mock(side_effect=RuntimeError("startup")))
    orch._progress.start_heartbeat(interval_s=60)
    try:
        with pytest.raises(RuntimeError, match="startup"):
            run()
        heartbeat.assert_called_once()
        assert orch._progress._hb_thread is None
    finally:
        orch._progress.stop_heartbeat()


def test_cleanup_preserves_another_owners_lock(tmp_path):
    own_root = tmp_path / "worktree"
    orch = Orchestrator(None)
    orch._repo_root = str(tmp_path)
    own_lock = EditLockState(repo_root=own_root)
    other_lock = EditLockState(repo_root=own_root)
    orch._edit_lock = own_lock
    orch.patcher = SimpleNamespace(tool_context=ToolContext(str(own_root), edit_lock=other_lock))
    try:
        orch._release_repair_resources()
        assert orch.patcher.tool_context.edit_lock is other_lock
        assert orch._edit_lock is None
    finally:
        orch.patcher.tool_context.edit_lock = None


def test_binding_close_failure_still_releases_worktree_lock(tmp_path, monkeypatch):
    orch = Orchestrator(None)
    orch._repo_root = str(tmp_path)
    own_root = tmp_path / "worktree"
    lock = EditLockState(repo_root=own_root)
    orch._edit_lock = lock
    orch.patcher = SimpleNamespace(tool_context=ToolContext(str(own_root), edit_lock=lock))
    binding = Mock()
    binding.close.side_effect = RuntimeError("close failure")
    orch._plan_binding = binding
    heartbeat = Mock()
    orch._progress = Mock(stop_heartbeat=heartbeat)
    state = RepairState(issue_input="public issue")
    monkeypatch.setattr(orch, "_repair_impl_with_plan", lambda *args: state)
    try:
        with pytest.raises(RuntimeError, match="close failure"):
            orch._repair_impl(state)
        assert orch.patcher.tool_context.edit_lock is None
        assert orch._plan_binding is None
        heartbeat.assert_called_once()
    finally:
        orch.patcher.tool_context.edit_lock = None


def test_exception_releases_resources(flow, monkeypatch):
    orch, run, _ = flow
    lock = EditLockState(repo_root=orch._repo_root)
    heartbeat = Mock(wraps=orch._progress.stop_heartbeat)
    binding = Mock()
    monkeypatch.setattr(orch._progress, "stop_heartbeat", heartbeat)

    def crash(state):
        orch._edit_lock = lock
        orch._plan_binding = binding
        orch._progress.start_heartbeat(interval_s=60)
        raise RuntimeError("patch failure")

    monkeypatch.setattr(orch, "_run_patcher", crash)
    try:
        with pytest.raises(RuntimeError, match="patch failure"):
            run()
        assert orch._edit_lock is None
        assert orch._edit_lock is None
        binding.close.assert_called_once()
        heartbeat.assert_called_once()
    finally:
        orch._progress.stop_heartbeat()


def test_every_attempt_has_an_owned_edit_policy(flow, monkeypatch):
    orch, run, _ = flow
    patcher = orch._run_patcher
    observed = []

    def inspect_policy(state):
        lock = orch._edit_lock
        assert isinstance(lock, EditLockState)
        assert lock.repo_root == Path(orch._repo_root)
        observed.append(lock)
        return patcher(state)

    monkeypatch.setattr(orch, "_run_patcher", inspect_policy)
    run()
    assert observed and orch._edit_lock is None
