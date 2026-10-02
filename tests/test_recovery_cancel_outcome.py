"""Strict public resume and shared recovery/cancel diagnostics."""

import json
from copy import deepcopy

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.run_coordination import RunCoordinator
from src.repair.checkpoint_load import save_repair_checkpoint
from src.repair.progress import ProgressEmitter
from src.repair_report import RepairReport
from tests.plan_l2_support import repair_fixture


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "json",
        "checksum",
        "identity",
        "objective",
        "shape",
        "no-envelope",
        "workspace",
        "schema",
    ],
)
def test_explicit_resume_invalid_checkpoint_never_enters_runtime(tmp_path, monkeypatch, damage):
    orch, seed, client = repair_fixture(tmp_path, full=True)
    if damage != "missing":
        path = save_repair_checkpoint(seed, str(tmp_path))
        if damage == "json":
            path.write_text("not json", encoding="utf-8")
        elif damage == "checksum":
            body = json.loads(path.read_text())
            body["checkpoint_checksum"] = "damaged"
            path.write_text(json.dumps(body))
        elif damage == "identity":
            seed.repair_run_id = "other-run"
            other = save_repair_checkpoint(seed, str(tmp_path))
            path.write_bytes(other.read_bytes())
            seed.repair_run_id = "plan-fixture-run"
        elif damage == "shape":
            seed.retry_count = "invalid"
            save_repair_checkpoint(seed, str(tmp_path))
        elif damage == "no-envelope":
            path.write_text(json.dumps({"retry_count": 0}))
        elif damage == "workspace":
            other_root = tmp_path / "other"
            other_root.mkdir()
            other = save_repair_checkpoint(seed, str(other_root))
            path.write_bytes(other.read_bytes())
        elif damage == "schema":
            from agent_runtime.session_contract import CheckpointEnvelope

            body = json.loads(path.read_text())
            envelope = CheckpointEnvelope.from_dict(body["checkpoint_envelope"])
            envelope.schema_version = "unsupported"
            envelope.seal()
            body["checkpoint_envelope"] = envelope.to_dict()
            body["checkpoint_checksum"] = envelope.checksum
            path.write_text(json.dumps(body))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    events = []
    orch._progress = ProgressEmitter(quiet=True, record=events.append)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid explicit resume entered runtime")

    monkeypatch.setattr(orch, "_begin_repair_trace", forbidden)
    issue = "another objective" if damage == "objective" else seed.issue_input
    result = orch.repair(issue, resume_run_id=seed.repair_run_id, repair_timeout_s=0)
    assert result.status == "recovery_required"
    assert client.session_usage["calls"] == 0
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    outcome = result.recovery_outcome
    assert outcome["stage"] == "checkpoint"
    assert (
        outcome["reason_code"]
        == {
            "missing": "resume_checkpoint_missing",
            "json": "resume_checkpoint_malformed",
            "checksum": "resume_checkpoint_integrity_failed",
            "identity": "resume_checkpoint_identity_mismatch",
            "objective": "resume_task_objective_mismatch",
            "shape": "resume_checkpoint_malformed",
            "no-envelope": "resume_checkpoint_integrity_failed",
            "workspace": "resume_checkpoint_identity_mismatch",
            "schema": "resume_checkpoint_schema_mismatch",
        }[damage]
    )
    assert not outcome["cleanup_confirmed"]
    assert outcome["next_action"] == "inspect_checkpoint"
    assert events[-1].extras["recovery"] == outcome


def test_public_resume_reports_unknown_resource_and_preserves_disk(tmp_path):
    orch, seed, client = repair_fixture(tmp_path, full=True)
    save_repair_checkpoint(seed, str(tmp_path))
    old = RunCoordinator(str(tmp_path), seed.repair_run_id, seed.repair_run_id)
    old.acquire()
    old.reconcile()
    old.register_resource(resource_id="unknown-write", kind="command", effect="write")
    old.finish("recovery_required")
    target = tmp_path / "value.py"
    target.write_text("unconfirmed change\n")
    result = orch.repair(seed.issue_input, resume_run_id=seed.repair_run_id, repair_timeout_s=0)
    assert result.status == "recovery_required" and client.session_usage["calls"] == 0
    assert target.read_text() == "unconfirmed change\n"
    outcome = result.recovery_outcome
    assert outcome["blocking_resources"][0]["resource_id"] == "unknown-write"
    assert outcome["blocking_resources"][0]["reason_code"] == "resource_adapter_missing"
    assert outcome["next_action"] == "verify_execution"
    detached = result.recovery_outcome
    detached["blocking_resources"].clear()
    assert result.recovery_outcome == outcome
    output = tmp_path / "report"
    output.mkdir()
    report = RepairReport(output, str(tmp_path))
    report.record_state(result, "host")
    assert json.loads((output / "result.json").read_text())["recovery"] == outcome
    text = (output / "report.md").read_text(encoding="utf-8")
    assert "unknown-write" in text and "resource_adapter_missing" in text


def test_cancel_request_and_cleanup_are_distinct_until_finalization(tmp_path):
    from src.repair.plan_binding import RepairPlanBinding

    orch, seed, _ = repair_fixture(tmp_path)
    token = CancellationToken()
    orch.patcher.cancel_token = token
    orch._repair_ctx.cancel_token = token
    binding = RepairPlanBinding(orch, seed, defer_plan=True)
    orch._plan_binding = binding
    try:
        token.cancel()
        assert seed.recovery_outcome["status"] == "cancel_requested"
        assert not seed.recovery_outcome["cleanup_confirmed"]
        assert orch._cancel_run_resources(seed, finalize=False)
        assert seed.recovery_outcome["status"] == "cancelling"
        assert seed.recovery_outcome["cleanup_confirmed"]
        assert seed.recovery_outcome["next_action"] == "wait"
        orch._end_repair_trace(seed)
        assert seed.recovery_outcome["status"] == "cancelled"
        assert seed.recovery_outcome["next_action"] == "new_run"
    finally:
        binding.close()


def test_projection_does_not_promote_cleanup_to_verified_write():
    from src.repair.recovery_outcome import build_recovery_outcome

    source = {
        "status": "active",
        "run_id": "r",
        "generation": 2,
        "resources": [
            {
                "resource_id": "w",
                "kind": "sandbox_call",
                "effect": "write",
                "status": "completed",
                "cleanup": "confirmed",
                "payload": {"secret": "x"},
            }
        ],
    }
    before = deepcopy(source)
    outcome = build_recovery_outcome(source, stage="resources", plan_report=None)
    assert outcome["cleanup_confirmed"]
    assert outcome["effects_verified"] is None
    assert outcome["next_action"] == "recheck_resume"
    assert "secret" not in str(outcome)
    assert source == before


def test_new_run_identity_does_not_load_checkpoint_and_conflicting_intent_is_rejected(
    tmp_path, monkeypatch
):
    from src.orchestrator import Orchestrator

    class Capture(Orchestrator):
        def _snapshot_repo(self):
            return {}

        def _repair_impl(self, state, initial_snapshot=None):
            assert self._repair_ctx.resume_checkpoint is None
            return state

    orch = Capture(None)
    orch._repo_root = str(tmp_path)
    monkeypatch.setattr(
        "src.repair.checkpoint_load.load_repair_checkpoint",
        lambda *a, **k: pytest.fail("new run loaded old state"),
    )
    assert orch.repair("issue", run_id="new-id", repair_timeout_s=0).repair_run_id == "new-id"
    with pytest.raises(ValueError, match="mutually exclusive"):
        orch.repair("issue", run_id="a", resume_run_id="b")


def test_cancel_before_execution_has_no_model_call_or_runtime_state(tmp_path):
    orch, seed, client = repair_fixture(tmp_path, full=True)
    token = CancellationToken()
    token.cancel()
    result = orch.repair(
        seed.issue_input, run_id=seed.repair_run_id, cancel_token=token, repair_timeout_s=0
    )
    assert result.status == "user_cancel"
    assert result.recovery_outcome["reason_code"] == "cancelled_before_execution"
    assert result.recovery_outcome["status"] == "cancelled"
    assert client.session_usage["calls"] == 0
    assert not (tmp_path / ".agent").exists()


def test_public_owner_conflict_reports_wait_without_mutating_winner(tmp_path):
    orch, seed, client = repair_fixture(tmp_path, full=True)
    save_repair_checkpoint(seed, str(tmp_path))
    winner = RunCoordinator(str(tmp_path), seed.repair_run_id, seed.repair_run_id)
    winner.acquire()
    winner.reconcile()
    before = winner.store.snapshot(seed.repair_run_id).to_dict()
    try:
        result = orch.repair(seed.issue_input, resume_run_id=seed.repair_run_id, repair_timeout_s=0)
        assert result.recovery_outcome["reason_code"] == "resume_owner_conflict"
        assert result.recovery_outcome["next_action"] == "wait"
        assert not result.recovery_outcome["cleanup_confirmed"]
        assert client.session_usage["calls"] == 0
        assert winner.store.snapshot(seed.repair_run_id).to_dict() == before
    finally:
        winner.finish("released")


def test_persisted_cancel_resume_finishes_cleanup_and_repeat_does_not_execute(tmp_path):
    orch, seed, client = repair_fixture(tmp_path, full=True)
    save_repair_checkpoint(seed, str(tmp_path))
    old = RunCoordinator(str(tmp_path), seed.repair_run_id, seed.repair_run_id)
    old.acquire()
    old.reconcile()
    old.register_resource(resource_id="prepared", kind="plan_attempt", effect="read")
    old.cancel("persisted")
    result = orch.repair(seed.issue_input, resume_run_id=seed.repair_run_id, repair_timeout_s=0)
    assert result.status == "user_cancel"
    assert result.recovery_outcome["status"] == "cancelled"
    assert result.recovery_outcome["cleanup_confirmed"]
    assert client.session_usage["calls"] == 0
    assert old.store.snapshot(seed.repair_run_id).cancel_request_id == "persisted"
    before = old.store.snapshot(seed.repair_run_id).to_dict()
    repeated = orch.repair(seed.issue_input, resume_run_id=seed.repair_run_id, repair_timeout_s=0)
    assert repeated.recovery_outcome["reason_code"] == "resume_run_terminal"
    assert repeated.recovery_outcome["next_action"] == "new_run"
    assert old.store.snapshot(seed.repair_run_id).to_dict() == before
    assert client.session_usage["calls"] == 0


def test_external_state_root_checkpoint_roundtrip_preserves_identity(tmp_path):
    from src.repair.checkpoint_load import load_repair_checkpoint

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "runtime"
    _, seed, _ = repair_fixture(workspace, full=True)
    path = save_repair_checkpoint(seed, str(workspace), state_root=str(state_root))
    before = path.read_bytes()
    loaded = load_repair_checkpoint(
        str(workspace),
        seed.repair_run_id,
        state_root=str(state_root),
        require_valid=True,
        issue=seed.issue_input,
    )
    assert loaded["state_root"] == str(state_root.resolve())
    assert path.read_bytes() == before
    assert not (workspace / ".agent").exists()


def test_repeated_unconfirmed_cancel_reports_resource_without_rollback(tmp_path):
    from src.repair.plan_binding import RepairPlanBinding

    orch, seed, _ = repair_fixture(tmp_path)
    binding = RepairPlanBinding(orch, seed, defer_plan=True)
    orch._plan_binding = binding
    binding.coordinator.register_resource(resource_id="residual", kind="command", effect="write")
    target = tmp_path / "value.py"
    target.write_text("unconfirmed write\n")
    try:
        durable_before = None
        for _ in range(2):
            assert not orch._cancel_run_resources(seed)
            assert seed.recovery_outcome["status"] == "recovery_required"
            assert not seed.recovery_outcome["cleanup_confirmed"]
            assert seed.recovery_outcome["blocking_resources"][0]["resource_id"] == "residual"
            assert target.read_text() == "unconfirmed write\n"
            current = binding.coordinator.store.snapshot(seed.repair_run_id).to_dict()
            if durable_before is not None:
                assert current == durable_before
            durable_before = current
            assert binding.coordinator.request_cancel() == current["cancel_request_id"]
        orch._end_repair_trace(seed)
        assert seed.recovery_outcome["status"] == "recovery_required"
    finally:
        binding.close()


def test_plan_recovery_projection_explains_unknown_and_confirmed_attempts():
    from src.repair.recovery_outcome import build_recovery_outcome

    source = {"status": "recovery_required", "run_id": "r"}
    outcome = build_recovery_outcome(source, stage="plan", plan_report={"uncertain": ["edit"]})
    assert outcome["plan"]["uncertain"] == ["edit"]
    assert outcome["effects_verified"] is False
    assert outcome["next_action"] == "verify_execution"
    confirmed = build_recovery_outcome(
        {"status": "active"}, stage="context", plan_report={"adopted": ["edit"]}
    )
    assert confirmed["effects_verified"] is True
    assert confirmed["effects_scope"] == "recovered_plan_attempts"


def test_old_generation_cancel_cannot_modify_replacement_owner(tmp_path):
    from agent_runtime.run_coordination import StaleGenerationError

    old = RunCoordinator(str(tmp_path), "task", "run")
    old.acquire()
    old.reconcile()
    old.register_resource(resource_id="unknown", kind="command", effect="write")
    assert old.cancel().status == "recovery_required"
    replacement = RunCoordinator(str(tmp_path), "task", "run")
    replacement.acquire()
    before = replacement.store.snapshot("run").to_dict()
    try:
        with pytest.raises(StaleGenerationError):
            old.request_cancel()
        with pytest.raises(StaleGenerationError):
            old.cancel()
        assert replacement.store.snapshot("run").to_dict() == before
    finally:
        replacement.finish("recovery_required")


def test_saved_recovery_display_is_discarded_when_restoring_checkpoint(tmp_path):
    from src.repair.pipeline import RepairPipelineMixin
    from src.state import RepairState

    saved = RepairState(issue_input="issue", repair_run_id="run")
    saved.control.recovery_outcome = {"status": "active", "cleanup_confirmed": True}
    restored = RepairState(issue_input="issue", repair_run_id="run")
    mixin = RepairPipelineMixin()
    mixin._repo_root = str(tmp_path)
    mixin._restore_state_from_repair_checkpoint(restored, saved.to_dict())
    assert restored.recovery_outcome == {}
