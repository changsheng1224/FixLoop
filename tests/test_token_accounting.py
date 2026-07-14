"""统一 token 会计单测：cache_read_tokens + cache_hit_rate 字段。"""

from agent_runtime.token_accounting import (
    build_report_token_fields,
    compute_cache_hit_rate,
    merge_session_snapshots,
    snapshot_from_session,
)


class TestCacheHitRate:
    def test_both_zero_is_zero(self):
        assert compute_cache_hit_rate(0, 0) == 0.0

    def test_all_hits(self):
        assert compute_cache_hit_rate(100, 0) == 1.0

    def test_all_misses(self):
        assert compute_cache_hit_rate(0, 100) == 0.0

    def test_mixed(self):
        assert compute_cache_hit_rate(700, 300) == 0.7


class TestSnapshotFromSession:
    def test_empty_session(self):
        snap = snapshot_from_session(None)
        assert snap["cache_read_tokens"] == 0
        assert snap["cache_hit_rate"] == 0.0

    def test_with_cache_tokens(self):
        usage = {
            "input_tokens": 1000, "output_tokens": 500,
            "cache_read_tokens": 800, "cache_creation_tokens": 200, "calls": 5,
        }
        snap = snapshot_from_session(usage)
        assert snap["cache_read_tokens"] == 800
        assert snap["cache_hit_rate"] == 0.8


class TestBuildReportTokenFields:
    def test_includes_cache_fields(self):
        usage = {
            "input_tokens": 1000, "output_tokens": 500,
            "cache_read_tokens": 600, "cache_creation_tokens": 400,
        }
        report = build_report_token_fields(usage)
        assert "cache_read_tokens" in report
        assert "cache_hit_rate" in report
        assert report["cache_read_tokens"] == 600


class TestMergeSnapshots:
    def test_sums_cache_tokens(self):
        s1 = snapshot_from_session({
            "input_tokens": 500, "output_tokens": 200,
            "cache_read_tokens": 300, "cache_creation_tokens": 100,
        })
        s2 = snapshot_from_session({
            "input_tokens": 300, "output_tokens": 100,
            "cache_read_tokens": 200, "cache_creation_tokens": 100,
        })
        merged = merge_session_snapshots(s1, s2)
        assert merged["cache_read_tokens"] == 500
        assert merged["cache_hit_rate"] == round(500 / 700, 4)
