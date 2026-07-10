"""Aggregate Gateway / Executor rejection stats across repair agents."""

from __future__ import annotations

import json
from pathlib import Path

REJECTION_SUMMARY_KEYS = (
    "permission_denied_by_tool",
    "tool_rejections_by_layer",
    "tool_rejections_by_gate",
    "permission_denied_by_agent",
)


def merge_count_maps(*maps: dict | None) -> dict:
    """Sum integer counts keyed by string."""
    merged: dict[str, int] = {}
    for mapping in maps:
        if not mapping:
            continue
        for key, value in mapping.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


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


def aggregate_rejection_from_agent_reports(reports: dict[str, dict]) -> dict:
    """Merge per-agent rejection fields into a repair-level summary."""
    if not reports:
        return {}

    layer_maps = []
    gate_maps = []
    tool_maps = []
    by_agent: dict[str, dict] = {}

    for agent, body in reports.items():
        denied = body.get("permission_denied_by_tool") or {}
        if denied:
            by_agent[agent] = dict(denied)
            tool_maps.append(denied)
        layer = body.get("tool_rejections_by_layer")
        if layer:
            layer_maps.append(layer)
        gate = body.get("tool_rejections_by_gate")
        if gate:
            gate_maps.append(gate)

    result: dict = {}
    merged_layer = merge_count_maps(*layer_maps)
    merged_gate = merge_count_maps(*gate_maps)
    merged_tool = merge_count_maps(*tool_maps)
    if merged_layer:
        result["tool_rejections_by_layer"] = merged_layer
    if merged_gate:
        result["tool_rejections_by_gate"] = merged_gate
    if merged_tool:
        result["permission_denied_by_tool"] = merged_tool
    if by_agent:
        result["permission_denied_by_agent"] = by_agent
    return result


def summarize_repair_rejections(run_dir: Path) -> dict:
    """Build repair-level rejection summary from agent reports in *run_dir*."""
    return aggregate_rejection_from_agent_reports(load_agent_reports_from_run(run_dir))


def gateway_denial_count(summary: dict) -> int:
    """Total Gateway-layer rejections from a repair summary."""
    layers = summary.get("tool_rejections_by_layer") or {}
    return int(layers.get("gateway", 0) or 0)
