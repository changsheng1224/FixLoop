"""regression_check.py 单测。"""

import json

from src.eval.metrics import compute_metrics
from src.eval.models import CaseResult
from src.eval.regression_check import RegressionChecker


def _summary(
    fixed: int,
    total: int,
    *,
    regression_indices: frozenset[int] | None = None,
) -> dict:
    flags = regression_indices or frozenset()
    results = [
        CaseResult(
            case_id=f"case_{i:03d}",
            fixed=i < fixed,
            introduced_regression=i in flags,
        )
        for i in range(total)
    ]
    return compute_metrics(results).summary


class TestRegressionChecker:
    def test_pass_when_metrics_stable(self):
        checker = RegressionChecker()
        baseline = _summary(8, 10)
        current = _summary(8, 10)
        result = checker.check({"summary": current}, {"summary": baseline})
        assert result.passed
        assert result.to_detected() is None
        md = checker.format_check_result(result)
        assert "**Status**: PASSED" in md

    def test_fail_fix_rate_drop_over_threshold(self):
        checker = RegressionChecker(fix_rate_drop_threshold_pp=5.0)
        baseline = _summary(8, 10)  # 80%
        current = _summary(2, 10)  # 20%, drop 60pp
        result = checker.check({"summary": current}, {"summary": baseline})
        assert not result.passed
        detected = result.to_detected()
        assert detected is not None
        assert detected.detected
        assert len(detected.issues) == 1
        assert detected.issues[0].metric == "fix_rate"
        assert "Fix Rate regression" in detected.issues[0].message
        md = checker.format_check_result(detected)
        assert "**Status**: FAILED" in md
        assert "50.0%" not in md or "80.0%" in md

    def test_fail_regression_rate_rise_over_threshold(self):
        checker = RegressionChecker(regression_rate_rise_threshold_pp=3.0)
        baseline = _summary(10, 10)
        current = _summary(10, 10, regression_indices=frozenset({0}))
        result = checker.check({"summary": current}, {"summary": baseline})
        assert not result.passed
        reg_issues = [i for i in result.issues if i.metric == "regression_rate"]
        assert reg_issues
        assert "Regression rate increased" in reg_issues[0].message

    def test_load_from_ablation_json_file(self, tmp_path):
        payload = {
            "runs": [
                {
                    "case_id": "case_001",
                    "fixed": True,
                    "retry_count": 0,
                    "actual_lines": 1,
                    "minimal_lines": 1,
                    "duration_ms": 1000,
                },
                {
                    "case_id": "case_002",
                    "fixed": False,
                    "retry_count": 0,
                    "actual_lines": 0,
                    "minimal_lines": 1,
                    "duration_ms": 1000,
                },
            ]
        }
        current_path = tmp_path / "current.json"
        baseline_path = tmp_path / "baseline.json"
        current_path.write_text(json.dumps(payload), encoding="utf-8")
        baseline_payload = dict(payload)
        baseline_payload["runs"] = [
            dict(payload["runs"][0]),
            dict(payload["runs"][0], case_id="case_002"),
        ]
        baseline_path.write_text(json.dumps(baseline_payload), encoding="utf-8")

        checker = RegressionChecker(fix_rate_drop_threshold_pp=5.0)
        result = checker.check(current_path, baseline_path)
        assert not result.passed
        assert result.issues[0].metric == "fix_rate"
