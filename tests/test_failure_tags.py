"""Repair 失败分类 tag 单测。"""

from src.repair.failure_tags import (
    FailureTag,
    allowed_patch_files,
    apply_failure_tags,
    classify_failure_tags,
)
from src.repair.termination import RepairTerminalStatus
from src.state import (
    CandidatePatch,
    RepairPlan,
    RepairState,
    RetrievedContext,
    SuspectLocation,
)


class TestAllowedPatchFiles:
    def test_collects_suspects_plan_and_tests(self):
        state = RepairState(
            issue_input="x",
            repair_plan=RepairPlan(suspect_files=["app.py"]),
            suspect_locations=[
                SuspectLocation(file_path="calc.py", start_line=1, end_line=2),
            ],
            retrieved_context=RetrievedContext(related_tests=["test_calc.py::test_add"]),
        )
        allowed = allowed_patch_files(state)
        assert allowed == {"app.py", "calc.py", "test_calc.py"}


class TestClassifyFailureTags:
    def test_success_returns_empty(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.FIXED,
            candidate_patches=[CandidatePatch(file_path="calc.py")],
        )
        assert classify_failure_tags(state) == []

    def test_timeout(self):
        state = RepairState(issue_input="x", status=RepairTerminalStatus.TIMEOUT)
        assert classify_failure_tags(state) == [FailureTag.TIMEOUT]

    def test_regression(self):
        state = RepairState(issue_input="x", status=RepairTerminalStatus.REGRESSION)
        assert classify_failure_tags(state) == [FailureTag.REGRESSION]

    def test_parse_fail_via_flag(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.EXHAUSTED,
            retry_count=3,
            node_timings={"patcher_parse_failed": True},
        )
        assert classify_failure_tags(state) == [FailureTag.PARSE_FAIL]

    def test_parse_fail_exhausted_no_patches(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.EXHAUSTED,
            retry_count=3,
            max_retries=3,
        )
        assert classify_failure_tags(state) == [FailureTag.PARSE_FAIL]

    def test_wrong_file(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.EXHAUSTED,
            retry_count=2,
            suspect_locations=[
                SuspectLocation(file_path="calc.py", start_line=1, end_line=1),
            ],
            candidate_patches=[CandidatePatch(file_path="other.py")],
        )
        assert classify_failure_tags(state) == [FailureTag.WRONG_FILE]

    def test_wrong_file_skipped_when_allowed_empty(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.EXHAUSTED,
            candidate_patches=[CandidatePatch(file_path="other.py")],
        )
        assert classify_failure_tags(state) == []

    def test_verify_exhausted_no_tag(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.EXHAUSTED,
            retry_count=3,
            suspect_locations=[
                SuspectLocation(file_path="calc.py", start_line=1, end_line=1),
            ],
            candidate_patches=[CandidatePatch(file_path="calc.py")],
        )
        assert classify_failure_tags(state) == []

    def test_timeout_beats_regression(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.TIMEOUT,
            node_timings={
                "baseline_pytest_code": 0,
                "post_patch_pytest_code": 1,
            },
        )
        assert classify_failure_tags(state) == [FailureTag.TIMEOUT]


class TestApplyFailureTags:
    def test_writes_tags_on_failure(self):
        state = RepairState(issue_input="x", status=RepairTerminalStatus.TIMEOUT)
        apply_failure_tags(state)
        assert state.failure_tags == ["timeout"]

    def test_clears_on_success(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.FIXED,
            candidate_patches=[CandidatePatch(file_path="a.py")],
            failure_tags=["timeout"],
        )
        apply_failure_tags(state)
        assert state.failure_tags == []
