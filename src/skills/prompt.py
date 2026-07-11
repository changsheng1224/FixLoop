"""Format matched Skill metadata for repair prompts."""

from __future__ import annotations

from typing import Literal

from src.prompts.loader import load_skill_miss_hint
from src.state import RepairPlan

SkillHintRole = Literal["localizer", "retriever", "patcher"]

_ROLE_CHAR_LIMITS: dict[SkillHintRole, int] = {
    "localizer": 400,
    "retriever": 600,
    "patcher": 1200,
}


def _indent_block(text: str, prefix: str = "  ") -> str:
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    return "\n".join(f"{prefix}{line}" for line in lines)


def _bullet_section(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    lines = [title]
    lines.extend(f"  - {item}" for item in items)
    return lines


def _tool_chain(plan: RepairPlan) -> str:
    if plan.skill.suggested_tools:
        return " → ".join(plan.skill.suggested_tools)
    return "（无）"


def _truncate(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def _format_localizer_hint(plan: RepairPlan) -> str:
    skill = plan.skill
    lines = [
        f"[Skill 工具序] {skill.matched_skill}",
        f"建议优先: {_tool_chain(plan)}",
    ]
    if skill.guidance:
        lines.append(f"定位提示: {skill.guidance[0]}")
    return _truncate("\n".join(lines), _ROLE_CHAR_LIMITS["localizer"])


def _format_retriever_hint(plan: RepairPlan) -> str:
    skill = plan.skill
    lines = [
        "[Skill 提示·检索]",
        f"策略: {skill.matched_skill}",
        f"工具序: {_tool_chain(plan)}",
    ]
    for item in skill.guidance[:2]:
        lines.append(f"  - {item}")
    return _truncate("\n".join(lines), _ROLE_CHAR_LIMITS["retriever"])


def _format_patcher_hint(plan: RepairPlan) -> str:
    skill = plan.skill
    lines = [
        "[Skill 提示]",
        f"策略: {skill.matched_skill}",
        f"建议工具链: {_tool_chain(plan)}",
    ]
    if skill.example_issue.strip():
        lines.append("参考 issue:")
        lines.append(_indent_block(skill.example_issue))
    lines.extend(_bullet_section("修复原则:", skill.guidance))
    lines.extend(_bullet_section("避免:", skill.avoid))
    if skill.example_patch.strip():
        lines.append(f"示例修复: {skill.example_patch.strip()}")
    return _truncate("\n".join(lines), _ROLE_CHAR_LIMITS["patcher"])


def format_skill_hint(plan: RepairPlan | None, role: SkillHintRole) -> str:
    """Render role-specific Skill hint block for L2 repair user prompts."""
    if not plan or not plan.skill.matched_skill:
        return ""

    if role == "localizer":
        return _format_localizer_hint(plan)
    if role == "retriever":
        return _format_retriever_hint(plan)
    return _format_patcher_hint(plan)


def format_skill_miss_hint(role: SkillHintRole) -> str:
    """Render generic Skill miss hint for *role* when match_skill returns None."""
    text = load_skill_miss_hint(role)
    limit = _ROLE_CHAR_LIMITS[role]
    return _truncate(text, limit)


def format_skill_hint_for_plan(plan: RepairPlan | None, role: SkillHintRole) -> str:
    """Render matched Skill hint or generic miss hint based on plan fallback state."""
    if not plan:
        return ""
    if plan.skill.matched_skill:
        return format_skill_hint(plan, role)
    if plan.skill.fallback_strategy in ("issue_type_routing", "generic_patcher"):
        return format_skill_miss_hint(role)
    return ""


def format_skill_hint_block(plan: RepairPlan | None) -> str:
    """Render full ``[Skill 提示]`` block for patcher (backward compatible)."""
    return format_skill_hint_for_plan(plan, "patcher")
