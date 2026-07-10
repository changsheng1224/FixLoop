"""Model call latency: TTFB (ttft_ms) and total round-trip timing."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class ModelCallTiming:
    """Single model HTTP/API round timing."""

    ttft_ms: int
    total_ms: int
    output_tokens: int = 0
    step: int = 0
    attempt: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def percentile_ms(values: list[int], percentile: float) -> int:
    """Return *percentile* (0–1) of millisecond samples."""
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    n = len(ordered)
    if percentile <= 0.5:
        return ordered[n // 2]
    idx = min(n - 1, int(n * percentile))
    return ordered[idx]


def _normalize_call(entry: ModelCallTiming | dict) -> dict:
    if isinstance(entry, ModelCallTiming):
        return entry.to_dict()
    return {
        "ttft_ms": int(entry.get("ttft_ms", 0) or 0),
        "total_ms": int(entry.get("total_ms", 0) or 0),
        "output_tokens": int(entry.get("output_tokens", 0) or 0),
        "step": int(entry.get("step", 0) or 0),
        "attempt": int(entry.get("attempt", 0) or 0),
    }


def summarize_ttft(calls: list[ModelCallTiming | dict]) -> dict:
    """Aggregate per-call timings into report-level TTFT fields."""
    if not calls:
        return {}

    ttfts: list[int] = []
    totals: list[int] = []
    by_call: list[dict] = []

    for index, raw in enumerate(calls):
        call = _normalize_call(raw)
        ttft = call["ttft_ms"]
        total = call["total_ms"]
        ttfts.append(ttft)
        totals.append(total)
        by_call.append(
            {
                "step": call["step"] or index + 1,
                "attempt": call["attempt"] or 1,
                "ttft_ms": ttft,
                "total_ms": total,
                "output_tokens": call["output_tokens"],
            }
        )

    return {
        "ttft_ms_p50": percentile_ms(ttfts, 0.5),
        "ttft_ms_max": max(ttfts),
        "ttft_ms_last": ttfts[-1],
        "model_call_ms_total": sum(totals),
        "ttft_ms_by_call": by_call,
    }


def build_report_latency_fields(calls: list[ModelCallTiming | dict] | None) -> dict:
    """Fields for report.json / agent_report.*.json (empty when no calls)."""
    return summarize_ttft(calls or [])


def read_http_body_with_timing(resp, chunk_size: int = 8192) -> tuple[bytes, int, int]:
    """Read an HTTP response body in chunks; return (body, ttft_ms, total_ms).

    *ttft_ms* is time from first ``read`` to first non-empty chunk (TTFB).
    """
    import time

    t0 = time.time()
    t_first: float | None = None
    chunks: list[bytes] = []
    while True:
        chunk = resp.read(chunk_size)
        if not chunk:
            break
        if t_first is None:
            t_first = time.time()
        chunks.append(chunk)
    t_end = time.time()
    if t_first is None:
        t_first = t_end
    ttft_ms = int((t_first - t0) * 1000)
    total_ms = int((t_end - t0) * 1000)
    return b"".join(chunks), ttft_ms, total_ms
