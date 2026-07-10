"""model_timing 单测。"""

import io
import time

from agent_runtime.model_timing import (
    ModelCallTiming,
    build_report_latency_fields,
    percentile_ms,
    read_http_body_with_timing,
    summarize_ttft,
)


class TestPercentileMs:
    def test_p50(self):
        assert percentile_ms([100, 200, 300], 0.5) == 200

    def test_empty(self):
        assert percentile_ms([], 0.5) == 0


class TestSummarizeTtft:
    def test_aggregates_calls(self):
        calls = [
            ModelCallTiming(ttft_ms=100, total_ms=400, output_tokens=10, step=1),
            ModelCallTiming(ttft_ms=300, total_ms=800, output_tokens=20, step=2),
        ]
        summary = summarize_ttft(calls)
        assert summary["ttft_ms_p50"] == 300
        assert summary["ttft_ms_max"] == 300
        assert summary["ttft_ms_last"] == 300
        assert summary["model_call_ms_total"] == 1200
        assert len(summary["ttft_ms_by_call"]) == 2

    def test_empty_returns_empty(self):
        assert summarize_ttft([]) == {}
        assert build_report_latency_fields([]) == {}
        assert build_report_latency_fields(None) == {}


class TestReadHttpBodyWithTiming:
    def test_single_chunk(self):
        payload = b'{"ok": true}'
        resp = io.BytesIO(payload)

        class Reader:
            def read(self, size=-1):
                return resp.read(size)

        body, ttft_ms, total_ms = read_http_body_with_timing(Reader())
        assert body == payload
        assert ttft_ms <= total_ms
        assert total_ms >= 0

    def test_multi_chunk(self):
        class Reader:
            def __init__(self):
                self._parts = [b"part1", b"part2"]
                self._index = 0

            def read(self, size=-1):
                if self._index >= len(self._parts):
                    return b""
                if self._index == 1:
                    time.sleep(0.02)
                part = self._parts[self._index]
                self._index += 1
                return part

        body, ttft_ms, total_ms = read_http_body_with_timing(Reader(), chunk_size=5)
        assert body == b"part1part2"
        assert ttft_ms <= total_ms
        assert total_ms >= 20
