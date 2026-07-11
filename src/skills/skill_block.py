"""Canonical Skill block renderer for repair L2 prompts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.state import RepairPlan

SkillHintRole = Literal["localizer", "retriever", "patcher", "verifier"]
SkillBlockSource = Literal["hit", "miss", "none"]

SKILL_BLOCK_HEADER = "[Skill 提示]"

ROLE_LABELS: dict[SkillHintRole, str] = {
    "localizer": "Localizer",
    "retriever": "Retriever",
    "patcher": "Patcher",
    "verifier": "Verifier",
}

_ROLE_CHAR_LIMITS: dict[SkillHintRole, int] = {
    "localizer": 400,
    "retriever": 600,
    "patcher": 1200,
    "verifier": 300,
}

_ROLE_MISS_DEFAULTS: dict[SkillHintRole, dict[str, object]] = {
    "localizer": {
        "summary": "未命中专用 Skill；请先从堆栈与嫌疑文件定位根因，再选择工具。",
        "guidance": [],
        "avoid": [],
    },
    "retriever": {
        "summary": "未命中专用 Skill；优先 find_test / read_file 收集断言与实现上下文。",
        "guidance": [],
        "avoid": [],
    },
    "patcher": {
        "summary": "未命中专用 Skill；以测试 assert 期望为准，做最小必要源码改动。",
        "guidance": [
            "只改提供的源文件，勿修改测试",
            "优先单行 diff，避免过大 multiline 补丁",
        ],
        "avoid": [],
    },
    "verifier": {
        "summary": "未命中专用 Skill；以 sandbox pytest 结果为准判定补丁是否通过。",
        "guidance": [
            "构建失败时检查依赖与语法",
            "测试失败时对照 assert 期望",
        ],
        "avoid": [],
    },
}


@dataclass(frozen=True)
class SkillBlockRender:
    text: str
    role: SkillHintRole
    source: SkillBlockSource


def _truncate(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


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


def _resolve_source(plan: RepairPlan | None) -> SkillBlockSource:
    if not plan:
        return "none"
    if plan.skill.matched_skill:
        return "hit"
    if plan.skill.fallback_strategy in ("issue_type_routing", "generic_patcher"):
        return "miss"
    return "none"


def _build_hit_lines(plan: RepairPlan, role: SkillHintRole) -> list[str]:
    skill = plan.skill
    lines = [
        SKILL_BLOCK_HEADER,
        f"角色: {ROLE_LABELS[role]}",
        f"策略: {skill.matched_skill}",
    ]

    if role == "localizer":
        lines.append(f"工具序: {_tool_chain(plan)}")
        if skill.guidance:
            lines.append(f"定位提示: {skill.guidance[0]}")
        return lines

    if role == "retriever":
        lines.append(f"工具序: {_tool_chain(plan)}")
        if skill.guidance:
            lines.append("检索要点:")
            for item in skill.guidance[:2]:
                lines.append(f"  - {item}")
        return lines

    if role == "verifier":
        if skill.guidance:
            lines.append(f"验证要点: {skill.guidance[0]}")
        if skill.avoid:
            lines.append(f"避免: {skill.avoid[0]}")
        return lines

    lines.append(f"工具序: {_tool_chain(plan)}")
    if skill.example_issue.strip():
        lines.append("参考 issue:")
        lines.append(_indent_block(skill.example_issue))
    lines.extend(_bullet_section("修复原则:", skill.guidance))
    lines.extend(_bullet_section("避免:", skill.avoid))
    if skill.example_patch.strip():
        lines.append(f"示例修复: {skill.example_patch.strip()}")
    return lines


def _build_miss_lines(role: SkillHintRole) -> list[str]:
    defaults = _ROLE_MISS_DEFAULTS[role]
    summary = str(defaults.get("summary", "") or "")
    guidance = list(defaults.get("guidance") or [])
    avoid = list(defaults.get("avoid") or [])
    lines = [
        SKILL_BLOCK_HEADER,
        f"角色: {ROLE_LABELS[role]}",
        "策略: generic",
        summary,
    ]
    lines.extend(_bullet_section("要点:", guidance))
    lines.extend(_bullet_section("避免:", avoid))
    return lines


def render_skill_hint_for_plan(
    plan: RepairPlan | None,
    role: SkillHintRole,
) -> SkillBlockRender:
    """Render unified Skill block with hit/miss metadata."""
    source = _resolve_source(plan)
    if source == "none":
        return SkillBlockRender(text="", role=role, source="none")

    if source == "hit":
        assert plan is not None
        lines = _build_hit_lines(plan, role)
    else:
        lines = _build_miss_lines(role)

    text = _truncate("\n".join(lines), _ROLE_CHAR_LIMITS[role])
    return SkillBlockRender(text=text, role=role, source=source)


def skill_hint_rendered_trace(render: SkillBlockRender) -> dict:
    """Trace payload for ``skill_hint_rendered`` events."""
    return {
        "role": render.role,
        "source": render.source,
        "char_len": len(render.text),
    }
