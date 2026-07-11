"""Resolve Skill match + miss fallback onto a RepairPlan."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from src.skills.fallback import SkillFallback, apply_skill_fallback, skill_matched_trace_payload
from src.skills.matcher import match_skill
from src.skills.models import MatchedSkill

if TYPE_CHECKING:
    from src.state import RepairPlan

MatchSkillFn = Callable[..., MatchedSkill | None]


def resolve_skill_for_plan(
    plan: RepairPlan,
    issue: str,
    *,
    language: str = "python",
    match_skill_fn: MatchSkillFn | None = None,
) -> tuple[MatchedSkill | None, SkillFallback]:
    """Match skill, apply to plan, and apply miss fallback routing."""
    matcher = match_skill_fn or match_skill
    matched = matcher(issue, language=language)
    if matched:
        matched.apply_to_plan(plan)
    fallback = apply_skill_fallback(plan)
    return matched, fallback


__all__ = ["resolve_skill_for_plan", "skill_matched_trace_payload"]
