"""EvalRunner 单测（Fake Orchestrator）。"""

import json
import tempfile
from pathlib import Path

import pytest

from src.eval.fake_runner import fake_orchestrator_factory
from src.eval.runner import DEFAULT_CASES_DIR, EvalRunner, build_eval_report
from src.eval.models import CaseResult

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"


class TestBuildEvalReport:
    def test_summary_aggregates(self):
        results = [
            CaseResult(
                case_id="case_001",
                issue_type="type_error",
                difficulty="easy",
                fixed=True,
                minimal_lines=1,
                actual_lines=1,
            ),
            CaseResult(
                case_id="case_002",
                issue_type="type_error",
                difficulty="medium",
                fixed=False,
            ),
        ]
        report = build_eval_report(results)
        assert report.summary["total"] == 2
        assert report.summary["fixed"] == 1
        assert report.summary["fix_rate"] == 0.5
        assert report.summary["first_attempt_rate"] == 0.5
        assert report.by_type["type_error"]["fixed"] == 1


class TestEvalRunnerFake:
    def test_run_all_fake_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = EvalRunner(
                orchestrator_factory=fake_orchestrator_factory(CASES_DIR),
                cases_dir=CASES_DIR,
                output_dir=tmp,
            )
            report = runner.run_all(["case_001", "case_002"])
            assert len(report.cases) == 2
            assert report.cases[0].fixed is True
            assert report.cases[1].fixed is True
            assert report.summary["fixed"] == 2

            out = Path(tmp) / "eval_report.json"
            assert out.is_file()
            data = json.loads(out.read_text(encoding="utf-8"))
            assert "summary" in data
            assert "by_type" in data
            assert len(data["cases"]) == 2

    def test_run_case_unknown(self):
        runner = EvalRunner(
            orchestrator_factory=fake_orchestrator_factory(CASES_DIR),
            cases_dir=CASES_DIR,
        )
        result = runner.run_case("case_999")
        assert result.fixed is False
        assert "unknown case" in result.error

    @pytest.mark.parametrize("case_id", [f"case_{i:03d}" for i in range(1, 11)])
    def test_each_case_fixes_with_fake(self, case_id):
        runner = EvalRunner(
            orchestrator_factory=fake_orchestrator_factory(CASES_DIR),
            cases_dir=CASES_DIR,
        )
        result = runner.run_case(case_id)
        assert result.fixed, f"{case_id} should fix with expected patch: {result.error}"
