"""CLI, state and reports share cancellation/recovery/timeout precedence."""

import pytest

from src.cli_exit_codes import REPAIR_EXIT_FAIL, repair_exit_code
from src.state import CandidatePatch, RepairState


def test_cancelled_run_with_patch_and_timeout_is_not_success_or_timeout():
    state = RepairState(
        issue_input="fix",
        status="user_cancel",
        candidate_patches=[CandidatePatch(file_path="value.py")],
        node_timings={},
        control={"repair_timeout": 1, "user_cancel": True},
    )
    assert repair_exit_code(state) == REPAIR_EXIT_FAIL


def test_recovery_required_with_timeout_preserves_recovery_failure():
    state = RepairState(
        issue_input="fix",
        status="recovery_required",
        node_timings={},
        control={"repair_timeout": 1, "coordination_status": "recovery_required"},
    )
    assert repair_exit_code(state) == REPAIR_EXIT_FAIL


@pytest.mark.parametrize("status", ["user_cancel", "recovery_required", "timeout"])
def test_report_failure_tags_and_cli_preserve_control_terminal_status(tmp_path, status):
    from src.repair.verification.termination import finalize_repair_state
    from src.repair_report import RepairReport
    from src.state import VerificationResult

    state = RepairState(
        issue_input="fix",
        status=status,
        phase="failed",
        verification_result=VerificationResult(all_passed=False, failure_logs=["AssertionError"]),
        control={"repair_timeout": 60},
    )
    finalize_repair_state(state)
    report = RepairReport(tmp_path, "fixture")
    report.record_state(state, "host")
    assert report.data["status"] == status
    assert report.data["category"] == status
    assert state.failure_tags == [status]
    assert repair_exit_code(state) == (3 if status == "timeout" else 1)


def test_static_checks_do_not_count_as_tested_repair(tmp_path):
    from src.repair_report import RepairReport
    from src.state import VerificationResult

    state = RepairState(
        issue_input="fix",
        status="fixed",
        candidate_patches=[CandidatePatch(file_path="a.py")],
        verification_result=VerificationResult(all_passed=True, total_tests=1, passed=1),
    )
    report = RepairReport(tmp_path, "fixture")
    report.record_state(state, "static")
    assert report.data["status"] == "pending_verify"
    report.record_state(state, "host")
    assert report.data["status"] == "fixed"


def test_report_resolves_control_flags_before_state_finalization(tmp_path):
    from src.repair_report import RepairReport

    state = RepairState(issue_input="fix", control={"user_cancel": True})
    report = RepairReport(tmp_path, "fixture")
    report.record_state(state, "host")
    assert report.data["status"] == report.data["runtime_status"] == "user_cancel"
    assert repair_exit_code(state) == REPAIR_EXIT_FAIL
