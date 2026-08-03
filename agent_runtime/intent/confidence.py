"""Explicit confidence scoring for intent routing (rule / embed / graph)."""

from __future__ import annotations

from typing import Any

from agent_runtime.intent.models import IntentGraph, IntentResult
from agent_runtime.intent.rules import RuleHit


def fuse_confidence(
    rule: RuleHit,
    *,
    embed_primary: str | None = None,
    embed_score: float | None = None,
    embed_margin: float | None = None,
) -> tuple[float, dict[str, float]]:
    """Return fused confidence and per-layer breakdown for one segment."""
    c_rule = float(rule.confidence)
    breakdown: dict[str, float] = {"c_rule": round(c_rule, 4)}
    if embed_primary is None or embed_score is None:
        breakdown["c_embed"] = 0.0
        breakdown["c_fuse"] = round(c_rule, 4)
        breakdown["margin"] = 0.0
        return c_rule, breakdown

    c_embed = float(embed_score)
    margin = float(embed_margin or 0.0)
    breakdown["c_embed"] = round(c_embed, 4)
    breakdown["margin"] = round(margin, 4)

    if c_rule >= 0.9:
        # strong rule: slight boost on agreement, ignore embed on conflict
        if embed_primary == rule.primary:
            fused = min(0.99, c_rule + 0.05 * max(margin, 0.0))
        else:
            fused = c_rule
            breakdown["conflict"] = 1.0
    elif embed_primary == rule.primary:
        fused = min(0.95, 0.5 * c_rule + 0.5 * c_embed)
    elif c_embed >= 0.55 and margin >= 0.08:
        fused = c_embed
        breakdown["embed_override"] = 1.0
    else:
        fused = c_rule
        breakdown["conflict"] = 1.0

    breakdown["c_fuse"] = round(fused, 4)
    return fused, breakdown


def graph_confidence(graph: IntentGraph) -> tuple[float, dict[str, float]]:
    """Weighted graph confidence over executable nodes."""
    execs = [n for n in graph.nodes if n.role == "executable"]
    if not execs:
        node = graph.nodes[0] if graph.nodes else None
        conf = float(node.confidence) if node else 0.0
        return conf, {"c_graph": conf, "min_node_conf": conf, "n_exec": 0.0}

    weights = []
    for n in execs:
        # higher-priority / repair-like intents weigh slightly more
        w = 1.0 + 0.15 * max(n.priority, 0)
        if n.primary.startswith("repair"):
            w += 0.1
        weights.append(w)
    total_w = sum(weights) or 1.0
    c_graph = sum(n.confidence * w for n, w in zip(execs, weights)) / total_w
    min_c = min(n.confidence for n in execs)
    return c_graph, {
        "c_graph": round(c_graph, 4),
        "min_node_conf": round(min_c, 4),
        "n_exec": float(len(execs)),
        "mean_node_conf": round(sum(n.confidence for n in execs) / len(execs), 4),
    }


def intents_snapshot(graph: IntentGraph) -> list[dict[str, Any]]:
    """Serializable per-intent split view for observability / clients."""
    rows: list[dict[str, Any]] = []
    for n in graph.nodes:
        if n.role == "constraint":
            continue
        rows.append(
            {
                "id": n.id,
                "primary": n.primary,
                "action": n.action,
                "role": n.role,
                "span": dict(n.span or {}),
                "text": (n.text or "")[:240],
                "confidence": round(float(n.confidence), 4),
                "parser": n.parser,
                "segment_index": n.segment_index,
            }
        )
    return rows


def apply_breakdown_to_result(
    result: IntentResult,
    *,
    segment_breakdowns: list[dict[str, float]] | None = None,
    split_strategy: str = "single",
) -> IntentResult:
    """Fill confidence_breakdown + raw_signals.intents / split_strategy."""
    c_graph, gbreak = graph_confidence(result.graph)
    breakdown = dict(gbreak)
    if segment_breakdowns:
        # average segment fuse scores
        fuses = [b.get("c_fuse", 0.0) for b in segment_breakdowns if b]
        if fuses:
            breakdown["c_seg_mean"] = round(sum(fuses) / len(fuses), 4)
        margins = [b.get("margin", 0.0) for b in segment_breakdowns if b]
        if margins:
            breakdown["margin"] = round(max(margins), 4)
        if any(b.get("conflict") for b in segment_breakdowns):
            breakdown["conflict"] = 1.0

    result.confidence = c_graph
    result.confidence_breakdown = breakdown
    signals = dict(result.raw_signals or {})
    signals["intents"] = intents_snapshot(result.graph)
    signals["split_strategy"] = split_strategy
    signals["min_node_conf"] = breakdown.get("min_node_conf", c_graph)
    result.raw_signals = signals
    return result
