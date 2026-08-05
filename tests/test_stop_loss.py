"""长程止损 StopLossTracker 测试。"""

from __future__ import annotations

from src.repair.degrade import should_degrade_to_baseline
from src.repair.failure_tags import FailureTag, classify_failure_tags
from src.repair.stop_loss import (
    StopLossReason,
    StopLossTracker,
    apply_stop_loss,
    has_stop_loss,
)
from src.repair.termination import RepairTerminalStatus, apply_terminal_status
from src.state import CandidatePatch, RepairState, VerificationResult


def _patch(path: str = "a.py", diff: str = "+x") -> CandidatePatch:
    return CandidatePatch(
        file_path=path,
        diff=diff,
        original_lines="old",
        patched_lines="new",
    )


def _vr(logs: list[str], *, total: int = 1, failed: int = 1) -> VerificationResult:
    return VerificationResult(
        all_passed=False,
        total_tests=total,
        failed=failed,
        failure_logs=logs,
    )


class TestStopLossTracker:
    def test_identical_patch_stops_on_second(self):
        tr = StopLossTracker()
        p = _patch(diff="+same")
        d1 = tr.record_verify_failure(_vr(["FAILED a.py::t - AssertionError"]), [p])
        assert not d1.stop
        assert d1.progress
        d2 = tr.record_verify_failure(_vr(["FAILED a.py::t - AssertionError"]), [p])
        assert d2.stop
        assert d2.reason == StopLossReason.IDENTICAL_PATCH

    def test_novel_patch_allows_long_horizon(self):
        tr = StopLossTracker()
        d1 = tr.record_verify_failure(
            _vr(["FAILED a.py::t - AssertionError: 1"]),
            [_patch(diff="+a")],
        )
        assert not d1.stop
        d2 = tr.record_verify_failure(
            _vr(["FAILED a.py::t - AssertionError: 2"]),
            [_patch(diff="+b")],
        )
        assert not d2.stop
        assert d2.progress
        d3 = tr.record_verify_failure(
            _vr(["FAILED a.py::t - AssertionError: 3"]),
            [_patch(diff="+c")],
        )
        assert not d3.stop

    def test_identical_verify_stops_at_three(self):
        """不同补丁但相同失败面 → 第 3 次止损。"""
        tr = StopLossTracker()
        logs = ["FAILED a.py::t - AssertionError: assert 1 == 2"]
        assert not tr.record_verify_failure(_vr(logs), [_patch(diff="+1")]).stop
        assert not tr.record_verify_failure(_vr(logs), [_patch(diff="+2")]).stop
        d3 = tr.record_verify_failure(_vr(logs), [_patch(diff="+3")])
        assert d3.stop
        assert d3.reason == StopLossReason.IDENTICAL_VERIFY

    def test_env_stops_at_two(self):
        tr = StopLossTracker()
        logs = ["ModuleNotFoundError: No module named 'x'"]
        vr = _vr(logs, total=0, failed=0)
        assert not tr.record_verify_failure(vr, [_patch(diff="+1")]).stop
        d2 = tr.record_verify_failure(vr, [_patch(diff="+2")])
        assert d2.stop
        assert d2.reason == StopLossReason.ENV

    def test_parse_thrash(self):
        tr = StopLossTracker()
        assert not tr.record_empty_patch(apply_failed=False).stop
        d2 = tr.record_empty_patch(apply_failed=False)
        assert d2.stop
        assert d2.reason == StopLossReason.PARSE_THRASH

    def test_apply_thrash(self):
        tr = StopLossTracker()
        assert not tr.record_empty_patch(apply_failed=True).stop
        d2 = tr.record_empty_patch(apply_failed=True)
        assert d2.stop
        assert d2.reason == StopLossReason.APPLY_THRASH


class TestStopLossWiring:
    def test_apply_marks_exhausted(self):
        state = RepairState(issue_input="x", max_retries=5, retry_count=1)
        apply_stop_loss(
            state,
            StopLossTracker().record_empty_patch(apply_failed=False),  # not stop
        )
        # force a real stop decision
        from src.repair.stop_loss import StopLossDecision

        apply_stop_loss(
            state,
            StopLossDecision(stop=True, reason=StopLossReason.NO_PROGRESS, hint="止损测试"),
        )
        assert has_stop_loss(state)
        apply_terminal_status(state)
        assert state.status == RepairTerminalStatus.EXHAUSTED
        assert "[止损]" in state.feedback

    def test_degrade_allowed_on_stop_loss_before_max_retries(self):
        state = RepairState(issue_input="x", max_retries=5, retry_count=2)
        state.node_timings["stop_loss"] = StopLossReason.IDENTICAL_VERIFY
        state.node_timings["post_patch_pytest_code"] = 1
        assert should_degrade_to_baseline(
            state, verification_enabled=True, cancelled=False, allow=True
        )

    def test_no_progress_tag(self):
        state = RepairState(issue_input="x")
        state.node_timings["stop_loss"] = StopLossReason.IDENTICAL_PATCH
        state.candidate_patches = [_patch()]
        state.verification_result = _vr(["FAILED a.py::t - AssertionError"])
        assert classify_failure_tags(state) == [FailureTag.NO_PROGRESS]

    def test_env_stop_still_verify_config_tag(self):
        state = RepairState(issue_input="x")
        state.node_timings["stop_loss"] = StopLossReason.ENV
        state.candidate_patches = [_patch()]
        state.verification_result = _vr(
            ["ModuleNotFoundError: No module named 'z'"], total=0, failed=0
        )
        assert classify_failure_tags(state) == [FailureTag.VERIFY_CONFIG]
