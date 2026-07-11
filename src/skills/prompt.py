"""Format matched Skill metadata for repair prompts."""

from __future__ import annotations

from src.state import RepairPlan


def _indent_block(text: str, prefix: str = "  ") -> str:
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    return "\n".join(f"{prefix}{line}" for line in lines)


def _bullet_section(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    lines = [title]
    lines.extend(f"  - {item}" for item in items)
    return lines


def format_skill_hint_block(plan: RepairPlan | None) -> str:
    """Render ``[Skill 提示]`` block for patcher user prompt."""
    if not plan or not plan.matched_skill:
        return ""

    tools = " → ".join(plan.suggested_tools) if plan.suggested_tools else "（无）"
    lines = [
        "[Skill 提示]",
        f"策略: {plan.matched_skill}",
        f"建议工具链: {tools}",
    ]
    if plan.skill_example_issue.strip():
        lines.append("参考 issue:")
        lines.append(_indent_block(plan.skill_example_issue))
    lines.extend(_bullet_section("修复原则:", plan.skill_guidance))
    lines.extend(_bullet_section("避免:", plan.skill_avoid))
    if plan.skill_example_patch.strip():
        lines.append(f"示例修复: {plan.skill_example_patch.strip()}")
    return "\n".join(lines)
