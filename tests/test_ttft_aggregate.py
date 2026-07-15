"""ttft_aggregate 单测。"""

import json

from src.repair.ttft_aggregate import aggregate_ttft_from_agent_reports, summarize_repair_ttft


class TestAggregateTtft:
    def test_merges_across_agents(self):
        reports = {
            "localizer": {
                "ttft_ms_p50": 100,
                "ttft_ms_max": 200,
                "model_call_ms_total": 500,
                "ttft_ms_by_call": [{"ttft_ms": 100, "total_ms": 500}],
            },
            "patcher": {
                "ttft_ms_p50": 300,
                "ttft_ms_max": 400,
                "model_call_ms_total": 800,
                "ttft_ms_by_call": [{"ttft_ms": 300, "total_ms": 800}],
            },
        }
        summary = aggregate_ttft_from_agent_reports(reports)
        assert summary["ttft_ms_p50"] == 300
        assert summary["ttft_ms_max"] == 300
        assert summary["model_call_ms_total"] == 1300
        assert "localizer" in summary["ttft_ms_by_agent"]

    def test_empty_reports(self):
        assert aggregate_ttft_from_agent_reports({}) == {}

    def test_ignores_summary_without_by_call(self):
        reports = {
            "patcher": {
                "ttft_ms_p50": 200,
                "ttft_ms_max": 200,
                "model_call_ms_total": 500,
            }
        }
        assert aggregate_ttft_from_agent_reports(reports) == {}


class TestSummarizeRepairTtft:
    def test_reads_agent_report_files(self, tmp_path):
        run_dir = tmp_path / "run-1"
        run_dir.mkdir()
        (run_dir / "agent_report.localizer.json").write_text(
            json.dumps(
                {
                    "ttft_ms_p50": 120,
                    "ttft_ms_max": 120,
                    "model_call_ms_total": 400,
                    "ttft_ms_by_call": [{"ttft_ms": 120, "total_ms": 400}],
                }
            ),
            encoding="utf-8",
        )
        summary = summarize_repair_ttft(run_dir)
        assert summary["ttft_ms_p50"] == 120
        assert summary["model_call_ms_total"] == 400
