"""Skill recall eval: match_skill vs metadata expected_skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.eval.models import CaseResult
from src.eval.runner import DEFAULT_CASES_DIR, build_eval_report
from src.eval.skill_metrics import (
    SkillEvalReport,
    compute_skill_metrics,
    format_skill_markdown,
    load_skill_eval_cases,
    run_skill_eval,
    skill_metrics_from_case_results,
)

EXPECTED_BY_CASE = {
    "case_001": "python_type_error_fix",
    "case_002": "python_type_error_fix",
    "case_003": "python_type_error_fix",
    "case_004": "python_import_error_fix",
    "case_005": "python_cannot_import_name_fix",
    "case_006": "python_logic_error_fix",
    "case_007": "python_attribute_error_fix",
    "case_008": "python_test_failure_fix",
    "case_009": "python_config_error_fix",
    "case_010": "python_composite_fix",
    "case_011": "python_syntax_error_fix",
    "case_012": "python_test_failure_fix",
    "case_013": "python_value_error_fix",
    "case_014": "python_type_error_fix",
    "case_015": "python_import_error_fix",
    "case_neg_001": None,
    "case_neg_002": None,
    "case_java_001": "java_type_error_fix",
    "case_java_002": "java_type_error_fix",
    "case_java_003": "java_type_error_fix",
}


class TestLoadSkillEvalCases:
    def test_loads_all_verified_cases(self):
        rows = load_skill_eval_cases(DEFAULT_CASES_DIR)
        assert len(rows) == 20
        assert {r.case_id for r in rows} == set(EXPECTED_BY_CASE)

    def test_each_row_has_expected_skill(self):
        rows = load_skill_eval_cases(DEFAULT_CASES_DIR)
        for row in rows:
            assert row.expected_skill == EXPECTED_BY_CASE[row.case_id]


class TestComputeSkillMetrics:
    def test_perfect_accuracy(self):
        rows = [
            {
                "case_id": "case_001",
                "expected_skill": "python_type_error_fix",
                "matched_skill": "python_type_error_fix",
                "skill_match": True,
            },
            {
                "case_id": "case_002",
                "expected_skill": "python_type_error_fix",
                "matched_skill": "python_type_error_fix",
                "skill_match": True,
            },
        ]
        metrics = compute_skill_metrics(rows)
        assert metrics["summary"]["total"] == 2
        assert metrics["summary"]["correct"] == 2
        assert metrics["summary"]["accuracy"] == 1.0
        assert metrics["summary"]["macro_recall"] == 1.0

    def test_mismatch_recorded_in_confusion(self):
        rows = [
            {
                "case_id": "case_x",
                "expected_skill": "python_type_error_fix",
                "matched_skill": "python_import_error_fix",
                "skill_match": False,
            },
        ]
        metrics = compute_skill_metrics(rows)
        assert metrics["summary"]["correct"] == 0
        confusion = metrics["confusion"]
        assert confusion["python_type_error_fix"]["python_import_error_fix"] == 1

    def test_no_match_expected(self):
        rows = [
            {
                "case_id": "case_neg",
                "expected_skill": None,
                "matched_skill": None,
                "skill_match": True,
            },
        ]
        metrics = compute_skill_metrics(rows)
        assert metrics["summary"]["correct"] == 1
        assert metrics["summary"]["no_match_count"] == 1


class TestRunSkillEval:
    def test_builtin_cases_all_match(self):
        report = run_skill_eval(DEFAULT_CASES_DIR)
        assert isinstance(report, SkillEvalReport)
        assert report.summary["total"] == 20
        assert report.summary["correct"] == 18
        assert report.summary["accuracy"] >= 0.88  # 15/17 with neg cases

    def test_filter_by_case_id(self):
        report = run_skill_eval(DEFAULT_CASES_DIR, case_ids=["case_001"])
        assert report.summary["total"] == 1
        assert report.cases[0].matched_skill == "python_type_error_fix"

    def test_to_dict_roundtrip(self, tmp_path: Path):
        report = run_skill_eval(DEFAULT_CASES_DIR, case_ids=["case_004", "case_005"])
        out = tmp_path / "skill_eval_report.json"
        out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "skill_metrics" in data
        assert data["skill_metrics"]["summary"]["total"] == 2

    def test_markdown_contains_by_skill_table(self):
        report = run_skill_eval(DEFAULT_CASES_DIR, case_ids=["case_001"])
        md = format_skill_markdown(report)
        assert "Skill Recall Eval" in md
        assert "python_type_error_fix" in md


class TestEvalReportIntegration:
    def test_skill_metrics_in_eval_report(self):
        results = [
            CaseResult(
                case_id="case_001",
                expected_skill="python_type_error_fix",
                matched_skill="python_type_error_fix",
                skill_match=True,
                skill_labeled=True,
            ),
            CaseResult(
                case_id="case_004",
                expected_skill="python_import_error_fix",
                matched_skill="python_import_error_fix",
                skill_match=True,
                skill_labeled=True,
            ),
        ]
        report = build_eval_report(results)
        assert report.skill_metrics
        assert report.skill_metrics["summary"]["accuracy"] == 1.0
        data = report.to_dict()
        assert "skill_metrics" in data

    def test_skill_metrics_from_case_results_helper(self):
        results = [
            CaseResult(
                case_id="case_001",
                expected_skill="python_type_error_fix",
                matched_skill="python_type_error_fix",
                skill_match=True,
                skill_labeled=True,
            ),
        ]
        metrics = skill_metrics_from_case_results(results)
        assert metrics["summary"]["correct"] == 1
