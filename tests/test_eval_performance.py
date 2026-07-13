"""eval_report 性能矩阵单测（V1.4-Bonus5c）。"""

from __future__ import annotations

from src.eval.metrics import _p50, compute_performance_matrix
from src.eval.models import CaseResult


# ---------------------------------------------------------------------------
# _p50
# ---------------------------------------------------------------------------


class TestP50:
    def test_empty(self):
        assert _p50([]) == 0

    def test_single(self):
        assert _p50([100]) == 100

    def test_odd_count(self):
        assert _p50([100, 200, 300]) == 200

    def test_even_count(self):
        assert _p50([100, 200, 300, 400]) == 250


# ---------------------------------------------------------------------------
# compute_performance_matrix
# ---------------------------------------------------------------------------


class TestPerformanceMatrix:
    def test_empty_results(self):
        p = compute_performance_matrix([])
        assert p["avg_context_tokens"] == 0
        assert p["avg_cache_hit_rate"] == 0.0
        assert p["p50_ttft_ms"] == 0
        assert p["avg_tool_steps"] == 0.0
        assert p["avg_repair_retries"] == 0.0

    def test_single_result_with_all_metrics(self):
        r = CaseResult(
            case_id="case_001",
            retry_count=2,
            agent_timings={
                "context_tokens": 1500,
                "cache_hit_rate": 0.75,
                "total_tool_steps": 8,
                "ttft_values": [120, 150, 130],
            },
        )
        p = compute_performance_matrix([r])
        assert p["avg_context_tokens"] == 1500.0
        assert p["avg_cache_hit_rate"] == 0.75
        assert p["p50_ttft_ms"] == 130  # p50 of [120, 130, 150]
        assert p["avg_tool_steps"] == 8.0
        assert p["avg_repair_retries"] == 2.0

    def test_multiple_results_averages(self):
        results = [
            CaseResult(case_id="a", retry_count=1, agent_timings={
                "context_tokens": 1000, "cache_hit_rate": 0.5,
                "total_tool_steps": 5, "ttft_values": [100],
            }),
            CaseResult(case_id="b", retry_count=3, agent_timings={
                "context_tokens": 2000, "cache_hit_rate": 0.8,
                "total_tool_steps": 10, "ttft_values": [200, 300],
            }),
        ]
        p = compute_performance_matrix(results)
        assert p["avg_context_tokens"] == 1500.0
        assert p["avg_cache_hit_rate"] == 0.65
        assert p["avg_tool_steps"] == 7.5
        assert p["avg_repair_retries"] == 2.0

    def test_ttft_across_cases_merged(self):
        """ttft 跨 case 合并计算 p50。"""
        results = [
            CaseResult(case_id="a", agent_timings={"ttft_values": [100, 300]}),
            CaseResult(case_id="b", agent_timings={"ttft_values": [200]}),
        ]
        p = compute_performance_matrix(results)
        assert p["p50_ttft_ms"] == 200  # sorted: [100, 200, 300]

    def test_missing_metrics_default_to_zero(self):
        r = CaseResult(case_id="case_001", agent_timings={})
        p = compute_performance_matrix([r])
        assert p["avg_context_tokens"] == 0.0
        assert p["avg_cache_hit_rate"] == 0.0
        assert p["p50_ttft_ms"] == 0
        assert p["avg_tool_steps"] == 0.0

    def test_partial_metrics(self):
        """部分字段存在时独立计算。"""
        r = CaseResult(
            case_id="case_001",
            retry_count=1,
            agent_timings={"context_tokens": 800, "total_tool_steps": 3},
        )
        p = compute_performance_matrix([r])
        assert p["avg_context_tokens"] == 800.0
        assert p["avg_cache_hit_rate"] == 0.0  # 无数据
        assert p["avg_tool_steps"] == 3.0
