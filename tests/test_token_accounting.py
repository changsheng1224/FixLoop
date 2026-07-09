"""统一 token 会计：session 快照、cache 命中率、report 字段合并。"""

from agent_runtime.token_accounting import (
    build_report_token_fields,
    compute_cache_hit_rate,
    merge_session_snapshots,
    parse_provider_usage,
    snapshot_from_session,
)


class TestParseProviderUsage:
    def test_anthropic_cache_fields(self):
        usage = parse_provider_usage(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 20,
            }
        )
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 20
        assert usage["cache_read_tokens"] == 80
        assert usage["cache_creation_tokens"] == 20

    def test_openai_compat_cache_aliases(self):
        usage = parse_provider_usage(
            {
                "input_tokens": 50,
                "output_tokens": 10,
                "prompt_cache_hit_tokens": 40,
                "prompt_cache_miss_tokens": 10,
            }
        )
        assert usage["cache_read_tokens"] == 40
        assert usage["cache_creation_tokens"] == 10

    def test_empty_usage(self):
        usage = parse_provider_usage(None)
        assert usage["input_tokens"] == 0
        assert usage["cache_read_tokens"] == 0


class TestCacheHitRate:
    def test_hit_rate(self):
        assert compute_cache_hit_rate(80, 20) == 0.8

    def test_zero_denominator(self):
        assert compute_cache_hit_rate(0, 0) == 0.0


class TestSnapshotFromSession:
    def test_includes_cache_hit_rate(self):
        snap = snapshot_from_session(
            {
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_read_tokens": 75,
                "cache_creation_tokens": 25,
                "calls": 3,
            }
        )
        assert snap["total_tokens"] == 125
        assert snap["cache_hit_rate"] == 0.75
        assert snap["api_calls"] == 3


class TestMergeSessionSnapshots:
    def test_sums_multiple_clients(self):
        merged = merge_session_snapshots(
            snapshot_from_session({"input_tokens": 10, "output_tokens": 5, "calls": 1}),
            snapshot_from_session(
                {
                    "input_tokens": 20,
                    "output_tokens": 8,
                    "cache_read_tokens": 15,
                    "cache_creation_tokens": 5,
                    "calls": 2,
                }
            ),
        )
        assert merged["input_tokens"] == 30
        assert merged["output_tokens"] == 13
        assert merged["cache_read_tokens"] == 15
        assert merged["cache_creation_tokens"] == 5
        assert merged["api_calls"] == 3
        assert merged["cache_hit_rate"] == 0.75


class TestBuildReportTokenFields:
    def test_prefers_api_tokens_over_context_estimate(self):
        fields = build_report_token_fields(
            {
                "input_tokens": 200,
                "output_tokens": 50,
                "cache_read_tokens": 160,
                "cache_creation_tokens": 40,
                "calls": 2,
            },
            {
                "total_tokens": 9999,
                "sections": {"prefix": 100, "request": 400},
                "prompt_budget": 6000,
                "cuts": ["history"],
            },
        )
        assert fields["total_tokens"] == 250
        assert fields["input_tokens"] == 200
        assert fields["cache_read_tokens"] == 160
        assert fields["cache_hit_rate"] == 0.8
        assert fields["token_usage"] == {"prefix": 100, "request": 400}
        assert fields["prompt_budget"] == 6000
        assert fields["budget_cuts"] == ["history"]

    def test_falls_back_to_context_when_no_api_tokens(self):
        fields = build_report_token_fields(
            {"input_tokens": 0, "output_tokens": 0, "calls": 0},
            {"total_tokens": 500, "sections": {"request": 500}},
        )
        assert fields["total_tokens"] == 500
