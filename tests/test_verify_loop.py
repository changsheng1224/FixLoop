"""验证循环分桶与反馈测试。"""

from __future__ import annotations

from src.orchestrator import Orchestrator
from src.repair.failure_tags import FailureTag, classify_failure_tags
from src.repair.verification.verify_diagnose import (
    VerifyBucket,
    diagnose_verification,
    enrich_related_tests_from_diagnosis,
    should_stop_on_env,
)
from src.state import CandidatePatch, RepairState, RetrievedContext, VerificationResult


class TestDiagnoseVerification:
    def test_module_not_found_is_env(self):
        result = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=["ModuleNotFoundError: No module named 'numpy'"],
        )
        diag = diagnose_verification(result)
        assert diag.bucket == VerifyBucket.ENV
        assert "环境" in diag.guidance or "依赖" in diag.guidance

    def test_sandbox_upload_incomplete_is_env(self):
        result = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=["sandbox upload did not complete"],
        )
        diag = diagnose_verification(result)
        assert diag.bucket == VerifyBucket.ENV
        assert "sandbox upload" in diag.reason or "env" in diag.reason

    def test_django_settings_is_env(self):
        result = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=[
                "django.core.exceptions.ImproperlyConfigured: "
                "Requested setting INSTALLED_APPS, but settings are not configured."
            ],
        )
        diag = diagnose_verification(result)
        assert diag.bucket == VerifyBucket.ENV

    def test_assertion_is_logic_with_nodeid(self):
        result = VerificationResult(
            all_passed=False,
            total_tests=3,
            failed=1,
            failure_logs=[
                "FAILED sympy/core/tests/test_expr.py::test_foo - AssertionError: assert 1 == 2"
            ],
        )
        diag = diagnose_verification(result)
        assert diag.bucket == VerifyBucket.LOGIC
        assert any("test_expr.py" in n for n in diag.failed_nodeids)
        assert "read_file" in diag.guidance

    def test_collect_error(self):
        result = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=["ERROR collecting tests/test_x.py"],
        )
        diag = diagnose_verification(result)
        assert diag.bucket == VerifyBucket.COLLECT

    def test_env_early_stop_threshold(self):
        assert not should_stop_on_env(consecutive_env=1)
        assert should_stop_on_env(consecutive_env=2)


class TestFailureTagUsesEnvBucket:
    def test_modulenotfound_tagged_verify_config_even_with_patches(self):
        state = RepairState(issue_input="x")
        state.candidate_patches = [
            CandidatePatch(file_path="a.py", diff="+x", original_lines=["a"], patched_lines=["b"])
        ]
        state.verification_result = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=["ModuleNotFoundError: No module named 'mpmath'"],
        )
        assert classify_failure_tags(state) == [FailureTag.VERIFY_CONFIG]


class TestBuildFeedbackBuckets:
    def test_feedback_includes_bucket_and_env_title(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=["ModuleNotFoundError: No module named 'foo'"],
        )
        state = RepairState(issue_input="bug")
        feedback = orch._build_feedback(result, state=state)
        assert "[验证分桶]" in feedback
        assert "bucket=env" in feedback
        assert "验证环境" in feedback
        assert state.node_timings.get("verify_bucket") == "env"

    def test_logic_feedback_points_to_read_file(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(
            all_passed=False,
            failed=1,
            total_tests=1,
            failure_logs=["FAILED pkg/tests/test_a.py::test_b - AssertionError"],
        )
        feedback = orch._build_feedback(result)
        assert "bucket=logic" in feedback
        assert "失败用例" in feedback
        assert "read_file" in feedback


class TestEnrichRelatedTests:
    def test_merges_nodeids(self):
        state = RepairState(issue_input="x")
        state.retrieved_context = RetrievedContext(
            related_tests=["already.py::t"],
        )
        diag = diagnose_verification(
            VerificationResult(
                all_passed=False,
                failed=1,
                total_tests=1,
                failure_logs=["FAILED new/test_x.py::test_y - AssertionError"],
            )
        )
        enrich_related_tests_from_diagnosis(state, diag)
        assert "already.py::t" in state.retrieved_context.related_tests
        assert any("test_x.py" in t for t in state.retrieved_context.related_tests)
