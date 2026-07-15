"""Format matched Skill metadata for repair prompts (compatibility layer)."""

from __future__ import annotations

from src.skills.skill_block import (
    SkillHintRole,
)
from src.skills.skill_block import (
    render_skill_hint_for_plan as _render_skill_hint_for_plan,
)
from src.state import RepairPlan

__all__ = [
    "SkillHintRole",
    "format_skill_hint",
    "format_skill_hint_block",
    "format_skill_hint_for_plan",
    "format_skill_miss_hint",
]


def format_skill_hint(plan: RepairPlan | None, role: SkillHintRole) -> str:
    """Render role-specific Skill hint when plan has a matched skill."""
    if not plan or not plan.skill.matched_skill:
        return ""
    return _render_skill_hint_for_plan(plan, role).text


def format_skill_miss_hint(role: SkillHintRole) -> str:
    """Render generic Skill miss hint for *role*."""
    from src.state import RepairPlan, SkillContext

    plan = RepairPlan(skill=SkillContext(fallback_strategy="issue_type_routing"))
    return _render_skill_hint_for_plan(plan, role).text


def format_skill_hint_for_plan(plan: RepairPlan | None, role: SkillHintRole) -> str:
    """Render matched Skill hint or generic miss hint based on plan fallback state."""
    return _render_skill_hint_for_plan(plan, role).text


def format_skill_hint_block(plan: RepairPlan | None) -> str:
    """Render full ``[Skill 提示]`` block for patcher (backward compatible)."""
    return format_skill_hint_for_plan(plan, "patcher")
