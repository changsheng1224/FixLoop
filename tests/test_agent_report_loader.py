"""agent_report_loader 单测。"""

from agent_runtime.model_timing import summarize_ttft
from src.repair.agent_report_loader import merge_agent_report, project_token_usage_by_agent


class TestMergeAgentReport:
    def test_merges_token_counters(self):
        old = {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20, "api_calls": 1}
        new = {"total_tokens": 50, "input_tokens": 40, "output_tokens": 10, "api_calls": 1}
        merged = merge_agent_report(old, new)
        assert merged["total_tokens"] == 150
        assert merged["api_calls"] == 2

    def test_merges_ttft_by_call_and_recomputes_summary(self):
        old = summarize_ttft([{"ttft_ms": 100, "total_ms": 400, "step": 1, "attempt": 1}])
        new = summarize_ttft([{"ttft_ms": 300, "total_ms": 800, "step": 2, "attempt": 1}])
        merged = merge_agent_report(old, new)
        assert len(merged["ttft_ms_by_call"]) == 2
        assert merged["ttft_ms_p50"] == 300
        assert merged["model_call_ms_total"] == 1200

    def test_project_token_usage(self):
        reports = {
            "patcher": {
                "total_tokens": 10,
                "input_tokens": 6,
                "output_tokens": 4,
                "api_calls": 1,
                "tool_steps": 2,
                "token_usage": {"api_input": 6},
            }
        }
        projected = project_token_usage_by_agent(reports)
        assert projected["patcher"]["total_tokens"] == 10
        assert projected["patcher"]["tool_steps"] == 2
