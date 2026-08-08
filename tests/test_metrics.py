"""metrics.py 单测。"""

import json

from src.eval.metrics import compute_metrics, format_markdown, write_metrics_markdown_from_report
from src.eval.models import CaseResult


def _sample_results() -> list[CaseResult]:
    return [
        CaseResult(
            case_id="case_001",
            issue_type="type_error",
            difficulty="easy",
            fixed=True,
            retry_count=0,
            actual_lines=2,
            minimal_lines=1,
            duration_ms=1500,
            total_tokens=1000,
            variant="full",
        ),
        CaseResult(
            case_id="case_002",
            issue_type="type_error",
            difficulty="medium",
            fixed=False,
            retry_count=2,
            actual_lines=4,
            minimal_lines=1,
            duration_ms=2500,
            introduced_regression=True,
            status="exhausted",
            failure_tags=["parse_fail", "timeout"],
            error="patcher: no patches in agent output",
            variant="full",
        ),
        CaseResult(
            case_id="case_001",
            issue_type="type_error",
            difficulty="easy",
            fixed=True,
            retry_count=0,
            actual_lines=1,
            minimal_lines=1,
            duration_ms=1200,
            total_tokens=800,
            variant="single",
        ),
    ]


class TestComputeMetrics:
    def test_summary_metrics(self):
        report = compute_metrics(_sample_results())
        summary = report.summary
        assert summary["total"] == 3
        assert summary["fixed"] == 2
        assert summary["fix_rate"] == round(2 / 3, 4)
        assert summary["first_attempt_rate"] == round(2 / 3, 4)
        assert summary["avg_retries"] == round(2 / 3, 2)
        assert summary["patch_precision"] == round((0.5 + 0.25 + 1.0) / 3, 4)
        assert summary["avg_duration_s"] == round((1500 + 2500 + 1200) / 3 / 1000, 2)
        assert summary["regression_rate"] == round(1 / 3, 4)
        assert summary["total_tokens"] == 1800
        assert summary["duration_ms_p50"] == 1500
        assert summary["duration_ms_p95"] == 2500
        assert summary["token_p50"] == 900
        assert summary["status_counts"] == {"fixed": 2, "exhausted": 1}
        assert summary["failure_tag_counts"] == {"parse_fail": 1, "timeout": 1}
        assert summary["failure_reason_counts"] == {"no_patch": 1}

    def test_groupings(self):
        report = compute_metrics(_sample_results())
        assert report.by_type["type_error"]["fixed"] == 2
        assert report.by_difficulty["easy"]["total"] == 2
        assert report.by_variant["full"]["fixed"] == 1
        assert report.by_variant["single"]["fix_rate"] == 1.0


class TestFormatMarkdown:
    def test_contains_required_sections(self):
        report = compute_metrics(_sample_results())
        md = format_markdown(report)
        assert "## Overall" in md
        assert "## By Variant" in md
        assert "## By Case" in md
        assert "## By Issue Type" in md
        assert "## By Difficulty" in md
        assert "## Failure Breakdown" in md
        assert "## Performance Detail" in md
        assert "## Confidence & Cost" in md
        assert "## Pass@k" in md
        assert "case_001" in md
        assert "full" in md
        assert "patch_precision" in md
        assert "failure_tags" in md


class TestLoadAndWriteMarkdown:
    def test_write_from_ablation_json(self, tmp_path):
        payload = {
            "runs": [
                {
                    "case_id": "case_001",
                    "issue_type": "type_error",
                    "difficulty": "easy",
                    "fixed": True,
                    "retry_count": 0,
                    "actual_lines": 1,
                    "minimal_lines": 1,
                    "duration_ms": 1000,
                    "variant": "full",
                    "run_index": 0,
                }
            ],
            "meta": {
                "variants": ["full"],
                "case_ids": ["case_001"],
                "repetitions": 1,
            },
        }
        json_path = tmp_path / "ablation_report.json"
        json_path.write_text(json.dumps(payload), encoding="utf-8")
        md_path = tmp_path / "report.md"
        write_metrics_markdown_from_report(json_path, md_path)
        text = md_path.read_text(encoding="utf-8")
        assert "## Run Notes" in text
        assert "## Overall" in text
        assert "case_001" in text
