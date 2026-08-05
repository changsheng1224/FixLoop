"""失败账本：假设版本化与回归缩 scope。"""

from __future__ import annotations

from src.repair.failure_ledger import (
    FailureLedger,
    build_ledger_prompt_block,
    record_verify_into_ledger,
    shrink_suspects_for_regression,
)
from src.state import CandidatePatch, RepairState, SuspectLocation, VerificationResult


class TestHypothesisLedger:
    def test_repeated_same_failure_negates_hypothesis(self):
        state = RepairState(issue_input="bug")
        state.candidate_patches = [
            CandidatePatch(
                file_path="a.py",
                diff="+x",
                original_lines=["a"],
                patched_lines=["b"],
            )
        ]
        vr = VerificationResult(
            all_passed=False,
            failed=1,
            total_tests=1,
            failure_logs=["FAILED t.py::test_x - AssertionError: assert 1 == 2"],
        )
        ledger = record_verify_into_ledger(state, result=vr, bucket="logic")
        assert ledger.active() is not None
        assert ledger.active().id == "H1"
        assert any("assert" in c.lower() or "FAILED" in c for c in ledger.active().counterexamples)

        # same failure again → negate
        ledger = record_verify_into_ledger(state, result=vr, bucket="logic")
        assert any(h.status == "negated" for h in ledger.hypotheses)
        assert "a.py" in ledger.negated_files

    def test_env_bucket_does_not_negate(self):
        state = RepairState(issue_input="x")
        state.candidate_patches = [
            CandidatePatch(file_path="a.py", diff="+1", original_lines=["a"], patched_lines=["b"])
        ]
        vr = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=["sandbox upload did not complete"],
        )
        ledger = record_verify_into_ledger(state, result=vr, bucket="env")
        assert not ledger.negated_files
        assert ledger.active() is None or ledger.active().status == "active"


class TestRegressionShrink:
    def test_marks_regression_files_and_shrinks(self):
        state = RepairState(issue_input="x")
        state.candidate_patches = [
            CandidatePatch(file_path="bad.py", diff="+1", original_lines=["a"], patched_lines=["b"])
        ]
        state.node_timings["baseline_pytest_code"] = 0
        state.node_timings["post_patch_pytest_code"] = 1
        vr = VerificationResult(
            all_passed=False,
            failed=2,
            total_tests=3,
            failure_logs=["FAILED other.py::test_y - AssertionError"],
        )
        ledger = record_verify_into_ledger(
            state, result=vr, bucket="logic", is_regression=True
        )
        assert "bad.py" in ledger.regression_files
        suspects = [
            SuspectLocation(file_path="bad.py", start_line=1, end_line=1),
            SuspectLocation(file_path="good.py", start_line=1, end_line=1),
        ]
        ordered = shrink_suspects_for_regression(suspects, ledger)
        assert ordered[0].file_path == "good.py"
        assert ordered[-1].file_path == "bad.py"

    def test_prompt_block_mentions_negated(self):
        ledger = FailureLedger()
        h = ledger.open_hypothesis(["a.py"], note="patch_attempt")
        ledger.negate(h, reason="repeated_counterexample")
        block = build_ledger_prompt_block(ledger)
        assert "FAILURE LEDGER" in block
        assert "已否定" in block
        assert "a.py" in block
