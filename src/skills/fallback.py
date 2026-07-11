"""Skill miss fallback: route prompt variants when match_skill returns None."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.repair.prompt_router import patcher_variant_for
from src.state import RepairPlan

SkillFallbackStrategy = Literal["hit", "issue_type_routing", "generic_patcher"]

GENERIC_FALLBACK_ISSUE_TYPES = frozenset({"unknown", "", "test_failure"})


@dataclass(frozen=True)
class SkillFallback:
    """Resolved behavior when Skill matching misses or hits."""

    strategy: SkillFallbackStrategy
    patcher_variant: str
    inject_miss_hint: bool


def resolve_skill_fallback(plan: RepairPlan) -> SkillFallback:
    """Choose fallback strategy from plan skill match and issue_type."""
    if plan.matched_skill:
        return SkillFallback(
            strategy="hit",
            patcher_variant=patcher_variant_for(plan),
            inject_miss_hint=False,
        )

    issue_type = (plan.issue_type or "").strip().lower()
    if issue_type in GENERIC_FALLBACK_ISSUE_TYPES:
        return SkillFallback(
            strategy="generic_patcher",
            patcher_variant="default",
            inject_miss_hint=True,
        )

    return SkillFallback(
        strategy="issue_type_routing",
        patcher_variant=patcher_variant_for(plan),
        inject_miss_hint=True,
    )


def apply_skill_fallback(plan: RepairPlan, *, matched: object | None) -> SkillFallback:
    """Write fallback strategy onto plan and adjust patcher prompt variant if needed."""
    fallback = resolve_skill_fallback(plan)
    plan.skill_fallback_strategy = fallback.strategy
    if fallback.strategy in ("issue_type_routing", "generic_patcher"):
        variants = dict(plan.prompt_variants or {})
        variants["patcher"] = fallback.patcher_variant
        plan.prompt_variants = variants
    return fallback


def skill_matched_trace_payload(matched, fallback: SkillFallback) -> dict:
    """Trace payload for orchestrator ``skill_matched`` event."""
    if matched is not None and getattr(matched, "to_trace_payload", None):
        payload = matched.to_trace_payload()
        payload["fallback_strategy"] = "hit"
        return payload
    return {
        "matched_skill": None,
        "fallback_strategy": fallback.strategy,
        "patcher_variant": fallback.patcher_variant,
        "inject_miss_hint": fallback.inject_miss_hint,
    }
