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
            CaseResult(case_id="case_001", fixed=True, retry_count=0, duration_ms=100, variant="full", run_index=0),
            CaseResult(case_id="case_002", fixed=False, retry_count=1, duration_ms=200, variant="full", run_index=0),
            CaseResult(case_id="case_001", fixed=True, retry_count=0, duration_ms=150, variant="single", run_index=0),
        ]
        report = build_ablation_report(results)
        assert report["summary_by_variant"]["full"]["total"] == 2
        assert report["summary_by_variant"]["full"]["fix_rate"] == 0.5
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
            assert set(report["summary_by_variant"].keys()) == {"full", "single", "no_retriever"}
            assert report["summary_by_variant"]["full"]["total"] == 4
            assert report["summary_by_variant"]["full"]["fixed"] == 4
            assert len(report["runs"]) == 12

            out = Path(tmp) / "ablation_report.json"
            assert out.is_file()
            data = json.loads(out.read_text(encoding="utf-8"))
            assert "summary_by_variant" in data
            assert data["runs"][0]["variant"] in {"full", "single", "no_retriever"}
            assert data["meta"]["status"] == "completed"
            assert data["meta"]["completed_runs"] == 12

            journal = Path(tmp) / "ablation_runs.jsonl"
            assert journal.is_file()
            assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == 12
