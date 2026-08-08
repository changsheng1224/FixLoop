"""Evaluation helpers for Skill execution contracts and outcome ablation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SkillExecutionEvalRow:
    case_id: str
    expected_status: str
    actual_status: str
    expected_error: str = ""
    actual_error: str = ""
    permission_escape: bool = False
    evidence_complete: bool = False
    trace_complete: bool = False


def execution_contract_metrics(rows: list[SkillExecutionEvalRow]) -> dict[str, float | int]:
    count = len(rows)
    denom = max(count, 1)
    return {
        "cases": count,
        "status_accuracy": sum(r.expected_status == r.actual_status for r in rows) / denom,
        "error_accuracy": sum(
            not r.expected_error or r.expected_error == r.actual_error for r in rows
        )
        / denom,
        "permission_escape_rate": sum(r.permission_escape for r in rows) / denom,
        "evidence_complete_rate": sum(r.evidence_complete for r in rows) / denom,
        "trace_complete_rate": sum(r.trace_complete for r in rows) / denom,
    }


def outcome_ablation_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate cohorts without treating correlation as causal attribution."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("cohort", "unknown"))].append(row)
    output: dict[str, Any] = {}
    for cohort, items in groups.items():
        denom = max(len(items), 1)
        output[cohort] = {
            "cases": len(items),
            "repair_success_rate": sum(bool(x.get("repair_succeeded")) for x in items) / denom,
            "verification_success_rate": sum(bool(x.get("verified")) for x in items) / denom,
            "avg_tool_calls": sum(int(x.get("tool_calls", 0)) for x in items) / denom,
            "avg_tokens": sum(int(x.get("tokens", 0)) for x in items) / denom,
            "avg_latency_ms": sum(int(x.get("latency_ms", 0)) for x in items) / denom,
            "invalid_change_rate": sum(bool(x.get("invalid_change")) for x in items) / denom,
        }
    return output
