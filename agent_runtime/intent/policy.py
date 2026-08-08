"""Risk, directive and signal-conflict policy for intent routing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.intent.models import IntentResult, RouteContext

INTENT_RISK: dict[str, str] = {
    "ask": "low",
    "help": "low",
    "explain": "low",
    "search": "low",
    "plan": "low",
    "review": "medium",
    "debug": "medium",
    "test": "medium",
    "repair_request": "high",
    "repair_issue": "high",
    "refactor": "high",
    "implement": "high",
    "remember": "high",
    "cancel": "high",
}

DEFAULT_RISK_THRESHOLDS = {"low": 0.55, "medium": 0.68, "high": 0.80}

_NO_WRITE = re.compile(
    r"(?i)(不要|别|无需|不用|先别|禁止).{0,10}(改|修改|修复|重构|实现|写入|动代码)|"
    r"(?:do\s+not|don'?t|without)\s+(?:change|modify|edit|fix|write|refactor)"
)
_EXPLAIN_ONLY = re.compile(
    r"(?i)(只|仅).{0,8}(解释|说明|分析|告诉)|only\s+(?:explain|analy[sz]e|tell)"
)
_WHY_ERROR = re.compile(r"(?i)(为什么|根因|原因|why|root\s+cause|error|exception|报错)")
_QUOTED_META = re.compile(
    r"(?i)(用户说|他说|她说|对方说|原文是|引用|日志里写着|日志显示|"
    r"quoted?|the\s+user\s+said|log\s+says|message\s+says).{0,120}"
    r"(修|fix|修改|remember|记住|重构|实现)"
)


@dataclass(frozen=True)
class DirectiveAnalysis:
    no_write: bool = False
    explain_only: bool = False
    quoted_instruction: bool = False
    preferred_primary: str = ""
    constraints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "no_write": self.no_write,
            "explain_only": self.explain_only,
            "quoted_instruction": self.quoted_instruction,
            "preferred_primary": self.preferred_primary,
            "constraints": list(self.constraints),
        }


def analyze_directives(text: str) -> DirectiveAnalysis:
    raw = text or ""
    no_write = bool(_NO_WRITE.search(raw))
    explain_only = bool(_EXPLAIN_ONLY.search(raw))
    quoted = bool(_QUOTED_META.search(raw)) or (
        ("「" in raw or '"' in raw or "'" in raw)
        and bool(re.search(r"(?i)(但|其实|实际|\bbut\b|\bactually\b)", raw))
    )
    preferred = ""
    if no_write or explain_only or quoted:
        preferred = "debug" if _WHY_ERROR.search(raw) else "explain"
    constraints = []
    if no_write:
        constraints.append("no_write")
    if explain_only:
        constraints.append("explain_only")
    if quoted:
        constraints.append("quoted_instruction_non_executable")
    return DirectiveAnalysis(
        no_write=no_write,
        explain_only=explain_only,
        quoted_instruction=quoted,
        preferred_primary=preferred,
        constraints=tuple(constraints),
    )


def risk_for(primary: str) -> str:
    return INTENT_RISK.get(str(primary), "medium")


def required_confidence(result: IntentResult, ctx: RouteContext) -> float:
    risk = risk_for(result.primary)
    configured = dict(DEFAULT_RISK_THRESHOLDS)
    configured.update(ctx.risk_thresholds or {})
    return max(float(ctx.tau_exec), float(configured.get(risk, ctx.tau_exec)))


def attach_risk_decision(result: IntentResult, ctx: RouteContext) -> dict[str, Any]:
    threshold = required_confidence(result, ctx)
    min_node = float(result.confidence_breakdown.get("min_node_conf", result.confidence))
    allow = result.confidence >= threshold and min_node >= threshold
    decision = {
        "risk": risk_for(result.primary),
        "threshold": round(threshold, 4),
        "confidence": round(float(result.confidence), 4),
        "min_node_conf": round(min_node, 4),
        "allow_execute": allow,
    }
    signals = dict(result.raw_signals or {})
    signals["risk_decision"] = decision
    result.raw_signals = signals
    return decision


@dataclass(frozen=True)
class ConflictDecision:
    winner: str
    resolution: str
    requires_clarify: bool
    candidates: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner,
            "resolution": self.resolution,
            "requires_clarify": self.requires_clarify,
            "candidates": [dict(item) for item in self.candidates],
        }


def arbitrate_conflict(
    *,
    rule_primary: str,
    rule_confidence: float,
    embed_primary: str | None,
    embed_score: float | None,
    embed_margin: float | None,
) -> ConflictDecision:
    candidates = [
        {"source": "rule", "primary": rule_primary, "confidence": round(rule_confidence, 4)}
    ]
    if embed_primary is not None:
        candidates.append(
            {
                "source": "embed",
                "primary": embed_primary,
                "confidence": round(float(embed_score or 0.0), 4),
                "margin": round(float(embed_margin or 0.0), 4),
            }
        )
    if not embed_primary or embed_primary == rule_primary:
        return ConflictDecision(rule_primary, "agreement", False, tuple(candidates))
    if rule_confidence >= 0.9:
        return ConflictDecision(
            rule_primary,
            "strong_rule",
            False,
            tuple(candidates),
        )
    if float(embed_score or 0.0) >= 0.65 and float(embed_margin or 0.0) >= 0.12:
        return ConflictDecision(
            embed_primary,
            "strong_embedding",
            False,
            tuple(candidates),
        )
    return ConflictDecision(rule_primary, "ambiguous", True, tuple(candidates))


__all__ = [
    "ConflictDecision",
    "DirectiveAnalysis",
    "analyze_directives",
    "arbitrate_conflict",
    "attach_risk_decision",
    "required_confidence",
    "risk_for",
]
