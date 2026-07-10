"""Aggregate TTFT latency fields across repair agent reports."""

from __future__ import annotations

from pathlib import Path

from agent_runtime.model_timing import percentile_ms
from src.repair.agent_report_loader import load_agent_reports_from_run


def aggregate_ttft_from_agent_reports(reports: dict[str, dict]) -> dict:
    """Merge per-agent TTFT summaries into repair-level fields."""
    if not reports:
        return {}

    all_ttfts: list[int] = []
    all_totals: list[int] = []
    by_agent: dict[str, dict] = {}

    for agent, body in reports.items():
        calls = body.get("ttft_ms_by_call") or []
        if not calls:
            continue
        by_agent[agent] = {
            "ttft_ms_p50": int(body.get("ttft_ms_p50", 0) or 0),
            "ttft_ms_max": int(body.get("ttft_ms_max", 0) or 0),
            "model_call_ms_total": int(body.get("model_call_ms_total", 0) or 0),
        }
        for entry in calls:
            all_ttfts.append(int(entry.get("ttft_ms", 0) or 0))
            all_totals.append(int(entry.get("total_ms", 0) or 0))

    if not all_ttfts:
        return {}

    result: dict = {
        "ttft_ms_p50": percentile_ms(all_ttfts, 0.5),
        "ttft_ms_max": max(all_ttfts),
        "model_call_ms_total": sum(all_totals),
    }
    if by_agent:
        result["ttft_ms_by_agent"] = by_agent
    return result


def summarize_repair_ttft(run_dir: Path) -> dict:
    """Build repair-level TTFT summary from agent reports in *run_dir*."""
    return aggregate_ttft_from_agent_reports(load_agent_reports_from_run(run_dir))
