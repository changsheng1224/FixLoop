"""Model call latency: TTFB (ttft_ms) and total round-trip timing."""

from __future__ import annotations

from collections.abc import Callable
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


def percentile_values(values: list[float | int], percentile: float) -> float | int:
    """Return *percentile* (0–1) of numeric samples (ms or seconds)."""
    if not values:
        return 0
    ordered = sorted(values)
    n = len(ordered)
    if percentile <= 0.5:
        return ordered[n // 2]
    idx = min(n - 1, int(n * percentile))
    return ordered[idx]


def percentile_ms(values: list[int], percentile: float) -> int:
    """Return *percentile* (0–1) of millisecond samples."""
    return int(percentile_values(values, percentile))


def normalize_timing_entry(entry: ModelCallTiming | dict) -> dict:
    if isinstance(entry, ModelCallTiming):
        return entry.to_dict()
    return {
        "ttft_ms": int(entry.get("ttft_ms", 0) or 0),
        "total_ms": int(entry.get("total_ms", 0) or 0),
        "output_tokens": int(entry.get("output_tokens", 0) or 0),
        "step": int(entry.get("step", 0) or 0),
        "attempt": int(entry.get("attempt", 0) or 0),
    }


def collect_client_timings(client) -> list[ModelCallTiming]:
    """Read per-call timings recorded on a model client."""
    timings = getattr(client, "last_call_timings", None) or []
    if timings:
        return list(timings)
    single = getattr(client, "last_call_timing", None)
    return [single] if single else []


def emit_model_timing_events(
    emit_fn: Callable[[str, dict], None],
    timings: list[ModelCallTiming | dict],
    *,
    default_attempt: int = 1,
) -> int:
    """Emit ``model_first_token`` / ``model_complete`` trace events; return ttft sum."""
    ttft_total = 0
    for index, raw in enumerate(timings):
        fields = normalize_timing_entry(raw)
        step = int(fields.get("step", 0) or index + 1)
        attempt = int(fields.get("attempt", 0) or default_attempt)
        ttft_ms = int(fields.get("ttft_ms", 0) or 0)
        total_ms = int(fields.get("total_ms", 0) or 0)
        output_tokens = int(fields.get("output_tokens", 0) or 0)
        ttft_total += ttft_ms
        emit_fn(
            "model_first_token",
            {"ttft_ms": ttft_ms, "step": step, "attempt": attempt},
        )
        emit_fn(
            "model_complete",
            {
                "total_ms": total_ms,
                "output_tokens": output_tokens,
                "step": step,
                "attempt": attempt,
            },
        )
    return ttft_total


def summarize_ttft(calls: list[ModelCallTiming | dict]) -> dict:
    """Aggregate per-call timings into report-level TTFT fields."""
    if not calls:
        return {}

    ttfts: list[int] = []
    totals: list[int] = []
    by_call: list[dict] = []

    for index, raw in enumerate(calls):
        call = normalize_timing_entry(raw)
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
