"""Aggregate Gateway / Executor rejection stats across repair agents."""

from __future__ import annotations

from pathlib import Path

from src.repair.agent_report_loader import load_agent_reports_from_run

GATEWAY_AGENT_ERROR_PREFIX = "gateway permission_denied:"


def format_gateway_denial_summary(tools: dict[str, int]) -> str:
    """Human-readable gateway denial summary for one agent."""
    parts = ", ".join(f"{tool}×{count}" for tool, count in sorted(tools.items()))
    return f"{GATEWAY_AGENT_ERROR_PREFIX} {parts}"


def apply_gateway_denials_to_agent_errors(
    agent_errors: dict,
    by_agent: dict[str, dict] | None,
) -> None:
    """Merge gateway permission_denied counts into RepairState.agent_errors."""
    if not by_agent:
        return
    for agent, tools in sorted(by_agent.items()):
        if not tools:
            continue
        msg = format_gateway_denial_summary(tools)
        existing = agent_errors.get(agent)
        if existing:
            if GATEWAY_AGENT_ERROR_PREFIX in existing:
                continue
            agent_errors[agent] = f"{existing}; {msg}"
        else:
            agent_errors[agent] = msg


def merge_count_maps(*maps: dict | None) -> dict:
    """Sum integer counts keyed by string."""
    merged: dict[str, int] = {}
    for mapping in maps:
        if not mapping:
            continue
        for key, value in mapping.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


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
