"""Load and merge per-agent ``agent_report.*.json`` from a shared repair run."""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.model_timing import summarize_ttft

TOKEN_COUNTER_KEYS = frozenset(
    {
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "api_calls",
        "tool_steps",
        "cache_read_tokens",
        "cache_creation_tokens",
    }
)

TTFT_SUMMARY_KEYS = frozenset(
    {
        "ttft_ms_p50",
        "ttft_ms_max",
        "ttft_ms_last",
        "model_call_ms_total",
        "ttft_ms_by_call",
    }
)


def load_agent_reports_from_run(run_dir: Path) -> dict[str, dict]:
    """Load full ``agent_report.{name}.json`` bodies from a shared run directory."""
    if not run_dir.is_dir():
        return {}
    reports: dict[str, dict] = {}
    for path in sorted(run_dir.glob("agent_report.*.json")):
        agent = path.stem.removeprefix("agent_report.")
        try:
            reports[agent] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return reports


def project_token_usage_by_agent(reports: dict[str, dict]) -> dict[str, dict]:
    """Project full agent reports to token/tool summary fields for repair report."""
    result: dict[str, dict] = {}
    for name, data in reports.items():
        result[name] = {
            "total_tokens": int(data.get("total_tokens", 0) or 0),
            "input_tokens": int(data.get("input_tokens", 0) or 0),
            "output_tokens": int(data.get("output_tokens", 0) or 0),
            "cache_read_tokens": int(data.get("cache_read_tokens", 0) or 0),
            "cache_creation_tokens": int(data.get("cache_creation_tokens", 0) or 0),
            "cache_hit_rate": float(data.get("cache_hit_rate", 0) or 0),
            "api_calls": int(data.get("api_calls", 0) or 0),
            "token_usage": data.get("token_usage") or {},
            "tool_steps": int(data.get("tool_steps", 0) or 0),
        }
    return result


def merge_agent_report(existing: dict, update: dict) -> dict:
    """Merge token counters and TTFT call lists between report writes."""
    merged = dict(existing)
    for key in TOKEN_COUNTER_KEYS:
        if key in existing or key in update:
            merged[key] = int(existing.get(key, 0) or 0) + int(update.get(key, 0) or 0)

    old_calls = list(existing.get("ttft_ms_by_call") or [])
    new_calls = list(update.get("ttft_ms_by_call") or [])
    if old_calls or new_calls:
        merged.update(summarize_ttft(old_calls + new_calls))
    else:
        for key in TTFT_SUMMARY_KEYS:
            if key in update:
                merged[key] = update[key]

    for key, value in update.items():
        if key in TOKEN_COUNTER_KEYS or key in TTFT_SUMMARY_KEYS:
            continue
        merged[key] = value
    return merged
