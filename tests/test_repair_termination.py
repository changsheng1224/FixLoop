"""Repair 终态 status 解析单测。"""

from src.repair.termination import (
    RepairTerminalStatus,
    apply_terminal_status,
    finalize_repair_state,
    introduced_regression,
    is_terminal,
    is_repair_success,
    regression_detected,
)
from src.state import CandidatePatch, RepairState


class TestRepairTerminalStatus:
    def test_is_terminal(self):
        assert is_terminal("fixed")
        assert is_terminal("timeout")
        assert is_terminal("patched")  # legacy 终态兼容
        assert not is_terminal("pending")

    def test_is_repair_success_fixed(self):
        state = RepairState(
            issue_input="x",
            status="fixed",
            candidate_patches=[CandidatePatch(file_path="a.py")],
        )
        assert is_repair_success(state)

    def test_is_repair_success_legacy_patched(self):
        state = RepairState(
            issue_input="x",
            status="patched",
            candidate_patches=[CandidatePatch(file_path="a.py")],
        )
        assert is_repair_success(state)


class TestApplyTerminalStatus:
    def test_keeps_fixed(self):
        state = RepairState(issue_input="x", status="fixed")
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.FIXED

    def test_user_cancel(self):
        state = RepairState(issue_input="x", status="pending")
        state.node_timings["user_cancel"] = True
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.USER_CANCEL

    def test_timeout_flag(self):
        state = RepairState(issue_input="x", status="failed")
        state.node_timings["repair_timeout"] = 180
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.TIMEOUT

    def test_exhausted(self):
        state = RepairState(issue_input="x", status="pending", retry_count=3, max_retries=3)
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.EXHAUSTED

    def test_regression(self):
        state = RepairState(
            issue_input="x",
            status="pending",
            retry_count=3,
            max_retries=3,
        )
        state.node_timings["baseline_pytest_code"] = 0
        state.node_timings["post_patch_pytest_code"] = 1
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.REGRESSION
        assert state.node_timings["introduced_regression"] is True

    def test_failed_fallback(self):
        state = RepairState(issue_input="x", status="pending", retry_count=0)
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.FAILED


class TestIntroducedRegression:
    def test_detects_green_to_red(self):
        state = RepairState(issue_input="x")
        state.node_timings["baseline_pytest_code"] = 0
        state.node_timings["post_patch_pytest_code"] = 2
        assert introduced_regression(state) is True

    def test_already_red_not_regression(self):
        state = RepairState(issue_input="x")
        state.node_timings["baseline_pytest_code"] = 1
        state.node_timings["post_patch_pytest_code"] = 1
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
        state.node_timings["repair_timeout"] = 60
        state.agent_errors["orchestrator"] = "repair timeout (60s)"
        finalize_repair_state(state)
        assert state.status == RepairTerminalStatus.TIMEOUT
        assert state.failure_tags == ["timeout"]
