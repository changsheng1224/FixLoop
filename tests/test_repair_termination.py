"""Repair 终态 status 解析单测。"""

from src.repair.verification.termination import (
    RepairTerminalStatus,
    apply_terminal_status,
    finalize_repair_state,
    has_actionable_patch,
    introduced_regression,
    is_repair_success,
    is_terminal,
    regression_detected,
    resolve_terminal_status,
)
from src.state import CandidatePatch, RepairState, RepairStatus, VerificationResult


class TestRepairTerminalStatus:
    def test_is_terminal(self):
        assert is_terminal("fixed")
        assert is_terminal("timeout")
        assert not is_terminal("patched")
        assert not is_terminal("pending")

    def test_is_repair_success_fixed(self):
        state = RepairState(
            issue_input="x",
            status="fixed",
            verification_result=VerificationResult(
                all_passed=True, total_tests=1, passed=1, execution_status="passed"
            ),
            candidate_patches=[CandidatePatch(file_path="a.py")],
        )
        assert is_repair_success(state)

    def test_pending_verify_is_terminal_but_not_success(self):
        state = RepairState(
            issue_input="x",
            status="pending_verify",
            candidate_patches=[CandidatePatch(file_path="a.py")],
        )
        assert is_terminal(state.status)
        assert not is_repair_success(state)
        assert has_actionable_patch(state)


class TestApplyTerminalStatus:
    def test_keeps_fixed(self):
        state = RepairState(
            issue_input="x",
            status="fixed",
            verification_result=VerificationResult(
                all_passed=True, total_tests=1, passed=1, execution_status="passed"
            ),
        )
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.FIXED

    def test_user_cancel(self):
        state = RepairState(issue_input="x", status="pending")
        state.control.user_cancel = True
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.USER_CANCEL

    def test_timeout_flag(self):
        state = RepairState(issue_input="x", status="failed")
        state.control.repair_timeout = 180
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.TIMEOUT

    def test_phase_timeout_flag(self):
        state = RepairState(issue_input="x", status="failed")
        state.control.phase_timeout = "localize"
        state.agent_errors["orchestrator"] = "phase timeout (localize, 60s budget, consumed 61.0s)"
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.TIMEOUT

    def test_exhausted(self):
        state = RepairState(issue_input="x", status="pending", retry_count=3, max_retries=3)
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.EXHAUSTED

    def test_regression(self):
        state = RepairState(issue_input="x", status="pending", retry_count=3, max_retries=3)
        state.control.baseline_pytest_code = 0
        state.control.post_patch_pytest_code = 1
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.REGRESSION
        assert state.control.introduced_regression is True

    def test_failed_fallback(self):
        state = RepairState(issue_input="x", status="pending", retry_count=0)
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.FAILED


class TestIntroducedRegression:
    def test_detects_green_to_red(self):
        state = RepairState(issue_input="x")
        state.control.baseline_pytest_code = 0
        state.control.post_patch_pytest_code = 2
        assert introduced_regression(state) is True

    def test_already_red_not_regression(self):
        state = RepairState(issue_input="x")
        state.control.baseline_pytest_code = 1
        state.control.post_patch_pytest_code = 1
        assert introduced_regression(state) is False


class TestRegressionDetected:
    def test_green_to_red(self):
        assert regression_detected(0, 1) is True

    def test_already_red_not_regression(self):
        assert regression_detected(1, 2) is False

    def test_none_codes(self):
        assert regression_detected(None, 0) is False


class TestFinalizeRepairState:
    def test_timeout_sets_failure_tag(self):
        state = RepairState(issue_input="x", status=RepairTerminalStatus.TIMEOUT)
        state.control.repair_timeout = 60
        state.agent_errors["orchestrator"] = "repair timeout (60s)"
        finalize_repair_state(state)
        assert state.status == RepairTerminalStatus.TIMEOUT
        assert state.failure_tags == ["timeout"]


class TestWaitingApproval:
    """沙箱审批暂停是一等状态：非终态、可恢复、退出码与失败区分。"""

    def test_not_terminal(self):
        assert not is_terminal(RepairStatus.WAITING_APPROVAL)

    def test_set_status_accepts_the_member(self):
        state = RepairState(issue_input="x")
        state.set_status(RepairStatus.WAITING_APPROVAL, "sandbox_approval_required")
        assert state.status == RepairStatus.WAITING_APPROVAL
        assert state.state_revision >= 1

    def test_resolve_preserves_the_pause_instead_of_reporting_failure(self):
        state = RepairState(issue_input="x", status=RepairStatus.WAITING_APPROVAL)
        assert resolve_terminal_status(state) == RepairStatus.WAITING_APPROVAL

    def test_pause_outranks_exhaustion(self):
        """重试上限同时用尽时仍是暂停，不得降级成 exhausted。"""
        state = RepairState(
            issue_input="x",
            status=RepairStatus.WAITING_APPROVAL,
            retry_count=3,
            max_retries=3,
        )
        assert resolve_terminal_status(state) == RepairStatus.WAITING_APPROVAL

    def test_timeout_supersedes_the_pause(self):
        state = RepairState(issue_input="x", status=RepairStatus.WAITING_APPROVAL)
        state.control.repair_timeout = 60
        assert resolve_terminal_status(state) == RepairStatus.TIMEOUT

    def test_cancel_supersedes_the_pause(self):
        state = RepairState(issue_input="x", status=RepairStatus.WAITING_APPROVAL)
        state.control.user_cancel = True
        assert resolve_terminal_status(state) == RepairStatus.USER_CANCEL

    def test_apply_terminal_status_keeps_the_pause(self):
        state = RepairState(issue_input="x", status=RepairStatus.WAITING_APPROVAL)
        apply_terminal_status(state)
        assert state.status == RepairStatus.WAITING_APPROVAL

    def test_checkpoint_payload_is_accepted(self):
        """暂停时写出的 checkpoint 必须通过持久化校验，否则无法续跑。"""
        from src.state import CURRENT_STATE_SCHEMA_VERSION, validate_state_payload

        payload = {
            "schema_version": CURRENT_STATE_SCHEMA_VERSION,
            "status": RepairStatus.WAITING_APPROVAL,
            "phase": "patch",
        }
        assert validate_state_payload(payload)["status"] == RepairStatus.WAITING_APPROVAL

    def test_pending_approval_id_survives_a_round_trip(self):
        state = RepairState(issue_input="x", status=RepairStatus.WAITING_APPROVAL)
        state.control.pending_approval_id = "approval-123"
        restored = RepairState.from_dict(state.to_dict())
        assert restored.control.pending_approval_id == "approval-123"

    def test_exit_code_is_distinct_from_failure(self):
        from src.cli_exit_codes import REPAIR_EXIT_APPROVAL, REPAIR_EXIT_FAIL, repair_exit_code

        paused = RepairState(issue_input="x", status=RepairStatus.WAITING_APPROVAL)
        failed = RepairState(issue_input="x", status=RepairStatus.FAILED)
        assert repair_exit_code(paused) == REPAIR_EXIT_APPROVAL
        assert repair_exit_code(paused) != REPAIR_EXIT_FAIL
        assert repair_exit_code(failed) == REPAIR_EXIT_FAIL
