"""AblationRunner 单测（Fake Orchestrator）。"""

import json
import tempfile
from pathlib import Path

from src.eval.ablation import AblationRunner, build_ablation_report
from src.eval.models import CaseResult
from src.eval.variants import build_ablation_variants

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"


class TestBuildAblationReport:
    def test_summary_by_variant(self):
        results = [
            CaseResult(
                case_id="case_001",
                fixed=True,
                retry_count=0,
                duration_ms=100,
                variant="full",
                run_index=0,
            ),
            CaseResult(
                case_id="case_002",
                fixed=False,
                retry_count=1,
                duration_ms=200,
                status="exhausted",
                failure_tags=["parse_fail"],
                error="baseline: no patches in agent output",
                actual_lines=0,
                minimal_lines=2,
                total_tokens=1200,
                variant="full",
                run_index=0,
            ),
            CaseResult(
                case_id="case_001",
                fixed=True,
                retry_count=0,
                duration_ms=150,
                variant="single",
                run_index=0,
            ),
        ]
        report = build_ablation_report(results)
        assert report["summary_by_variant"]["full"]["total"] == 2
        assert report["summary_by_variant"]["full"]["fix_rate"] == 0.5
        assert report["summary_by_variant"]["full"]["status_counts"]["exhausted"] == 1
        assert report["summary_by_variant"]["full"]["failure_tag_counts"]["parse_fail"] == 1
        assert report["summary_by_variant"]["full"]["failure_reason_counts"]["no_patch"] == 1
        assert report["summary_by_variant"]["full"]["duration_ms_p95"] == 200
        assert report["summary_by_case"]["case_001"]["total"] == 2
        assert report["summary_by_variant"]["single"]["fixed"] == 1
        assert len(report["runs"]) == 3


class TestAblationRunnerFake:
    def test_run_fake_ablation(self):
        with tempfile.TemporaryDirectory() as tmp:
            variants = build_ablation_variants(fake=True, cases_dir=CASES_DIR)
            runner = AblationRunner(
                variants=variants,
                cases_dir=CASES_DIR,
                output_dir=tmp,
            )
            report = runner.run(case_ids=["case_001", "case_002"], repetitions=2)
            assert set(report["summary_by_variant"].keys()) == {
                "full",
                "single",
                "no_retriever",
                "naive",
            }
            assert report["summary_by_variant"]["full"]["total"] == 4
            assert report["summary_by_variant"]["full"]["fixed"] == 4
            assert len(report["runs"]) == 16

            out = Path(tmp) / "ablation_report.json"
            assert out.is_file()
            data = json.loads(out.read_text(encoding="utf-8"))
            assert "summary_by_variant" in data
            assert data["runs"][0]["variant"] in {"full", "single", "no_retriever", "naive"}
            assert data["meta"]["status"] == "completed"
            assert data["meta"]["completed_runs"] == 16

            journal = Path(tmp) / "ablation_runs.jsonl"
            assert journal.is_file()
            assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == 16

    def test_run_normalizes_blank_status_results(self, tmp_path, monkeypatch):
        from src.eval.runner import EvalRunner

        class BlankStatusRunner(EvalRunner):
            def run_case(self, case_id: str):
                return CaseResult(
                    case_id=case_id,
                    fixed=False,
                    status="",
                    duration_ms=1,
                    total_tokens=0,
                )

        monkeypatch.setattr("src.eval.ablation.EvalRunner", BlankStatusRunner)
        variants = {"full": lambda repo_path: object()}
        runner = AblationRunner(
            variants=variants,
            cases_dir=CASES_DIR,
            output_dir=tmp_path,
        )
        report = runner.run(case_ids=["case_001"], repetitions=1)
        assert report["runs"][0]["status"] == "failed"
        assert report["summary_by_variant"]["full"]["status_counts"]["failed"] == 1
