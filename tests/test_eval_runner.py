"""EvalRunner 单测（Fake Orchestrator）。"""

import json
import tempfile
from pathlib import Path

import pytest

from src.eval.fake_runner import fake_orchestrator_factory
from src.eval.models import CaseResult
from src.eval.runner import EvalRunner, _copy_case_repo, build_eval_report, collect_repo_diff

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"


def _case_count() -> int:
    return sum(1 for p in CASES_DIR.iterdir() if p.is_dir() and p.name.startswith("case_"))


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


class TestCollectRepoDiff:
    def test_skips_agent_and_pytest_artifacts(self, tmp_path):
        original = tmp_path / "orig"
        modified = tmp_path / "mod"
        for repo in (original, modified):
            repo.mkdir()
            (repo / "app.py").write_text("a = 1\n", encoding="utf-8")
        (modified / "app.py").write_text("a = 2\n", encoding="utf-8")
        agent_dir = modified / ".agent" / "runs" / "x"
        agent_dir.mkdir(parents=True)
        (agent_dir / "report.json").write_text("{}", encoding="utf-8")
        cache_dir = modified / ".pytest_cache" / "v"
        cache_dir.mkdir(parents=True)
        (cache_dir / "cache").write_text("x", encoding="utf-8")

        diff = collect_repo_diff(original, modified)
        assert "app.py" in diff
        assert ".agent" not in diff
        assert ".pytest_cache" not in diff
        assert diff.count("\n+") == 1 or "a = 2" in diff

    def test_copy_case_repo_skips_pytest_cache(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / ".pytest_cache" / "v").mkdir(parents=True)
        (src / ".pytest_cache" / "v" / "cache").write_text("x", encoding="utf-8")
        (src / "app.py").write_text("print('ok')\n", encoding="utf-8")

        _copy_case_repo(src, dst)

        assert (dst / "app.py").is_file()
        assert not (dst / ".pytest_cache").exists()


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


class TestRunnerCli:
    def test_ci_flag_runs_fake_eval(self, tmp_path, monkeypatch):
        from src.eval import __main__ as runner_main

        out = tmp_path / "ci"
        monkeypatch.chdir(tmp_path)
        code = runner_main.main(["--ci", "--output", str(out)])
        assert code in (0, 1)
        report_path = out / "eval_report.json"
        assert report_path.is_file()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == _case_count()
        assert data["summary"]["fix_rate"] >= 0.5
