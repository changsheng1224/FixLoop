"""Intent Router SLO evaluation used by metrics and CI quality gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IntentSLOPolicy:
    route_latency_ms: float = 100.0
    severe_misroute_rate: float = 0.01
    clarify_recall: float = 0.92
    calibration_ece: float = 0.08
    exact_graph_match_rate: float = 0.90


def evaluate_route_slo(
    *,
    latency_ms: float,
    risk_decision: dict[str, Any] | None = None,
    llm_runtime: dict[str, Any] | None = None,
    embed_skipped: bool = False,
    action: str = "",
    policy: IntentSLOPolicy | None = None,
) -> list[str]:
    policy = policy or IntentSLOPolicy()
    violations: list[str] = []
    if latency_ms > policy.route_latency_ms:
        violations.append("route_latency")
    risk = risk_decision or {}
    if (
        risk.get("risk") == "high"
        and not risk.get("allow_execute", False)
        and action not in {"clarify", "reject", "noop_cancel"}
    ):
        violations.append("high_risk_below_threshold")
    fallback = llm_runtime or {}
    if fallback.get("fallback_reason") in {
        "timeout",
        "provider_error",
        "circuit_open",
        "rate_limited",
        "budget_exceeded",
        "deadline_exceeded",
        "schema_rejected",
    }:
        violations.append("llm_fallback_degraded")
    if embed_skipped:
        violations.append("embedding_unavailable")
    return violations


def evaluate_eval_slo(
    summary: dict[str, Any], policy: IntentSLOPolicy | None = None
) -> dict[str, Any]:
    policy = policy or IntentSLOPolicy()
    checks = {
        "severe_misroute_rate": float(summary.get("severe_misroute_rate", 1.0))
        <= policy.severe_misroute_rate,
        "clarify_recall": float(summary.get("clarify_recall", 0.0)) >= policy.clarify_recall,
        "ece": float(summary.get("ece", 1.0)) <= policy.calibration_ece,
        "exact_graph_match_rate": float(summary.get("exact_graph_match_rate", 0.0))
        >= policy.exact_graph_match_rate,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "policy": policy.__dict__.copy(),
    }


__all__ = ["IntentSLOPolicy", "evaluate_eval_slo", "evaluate_route_slo"]
