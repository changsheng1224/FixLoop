"""Patcher repair task 块组装（从 Orchestrator 抽出）。"""

from __future__ import annotations

from typing import Callable

from src.blackboard import Blackboard
from src.prompts.repair_tasks import build_patcher_variables
from src.repair.blackboard_merge import read_suspects_from_blackboard
from src.repair.blackboard_subscribe import render_patcher_prefix_blocks
from src.repair.prompt_router import collect_patcher_user_hints, is_composite_multi_file
from src.repair.repair_context_blocks import build_repair_context_blocks
from src.repair.suspect_blocks import render_suspects_diff_only, render_suspects_with_snippets
from src.skills.skill_block import SkillBlockRender, render_skill_hint_for_plan
from src.state import RepairPlan, RetrievedContext, SuspectLocation

__all__ = ["assemble_patcher_variables", "build_issue_hints"]


def build_issue_hints(plan: RepairPlan | None, issue: str) -> list[str]:
    return collect_patcher_user_hints(plan, issue)


def assemble_patcher_variables(
    *,
    suspects: list[SuspectLocation],
    context: RetrievedContext | None,
    feedback: str,
    plan: RepairPlan | None,
    issue: str,
    read_snippet: Callable[[str, int, int], str],
    read_test_context: Callable[
        [RetrievedContext | None, list[SuspectLocation], RepairPlan | None],
        list[str],
    ],
    fallback_suspects: Callable[[RepairPlan, str], list[SuspectLocation]],
    skill_render: SkillBlockRender | None = None,
    blackboard: Blackboard | None = None,
    diff_only: bool = False,
    read_line_range: Callable[[str, int, int], str] | None = None,
) -> tuple[dict[str, str], SkillBlockRender, dict | None]:
    subscribe_meta: dict | None = None
    effective_suspects: list[SuspectLocation]

    if blackboard is not None:
        prefix_blocks = render_patcher_prefix_blocks(
            blackboard,
            read_snippet=read_snippet,
            read_test_context=read_test_context,
            plan=plan,
        )
        effective_suspects = read_suspects_from_blackboard(blackboard)
        if not effective_suspects and plan:
            effective_suspects = fallback_suspects(plan, issue)

        suspects_block = prefix_blocks.suspects_block
        if not suspects_block:
            if diff_only and read_line_range is not None:
                suspects_block = render_suspects_diff_only(
                    effective_suspects, read_line_range
                )
            else:
                suspects_block, _ = render_suspects_with_snippets(effective_suspects, read_snippet)

        test_text = prefix_blocks.test_blocks
        if not test_text:
            test_blocks = read_test_context(context, effective_suspects, plan)
            if test_blocks:
                test_text = "相关测试文件（补丁必须通过这些 assert）:\n" + "\n".join(test_blocks)

        if prefix_blocks.scratch_block and not feedback:
            feedback = prefix_blocks.scratch_block

        subscribe_meta = {
            "subscribed_prefixes": prefix_blocks.subscribed_prefixes,
            "entry_counts": prefix_blocks.entry_counts,
        }
    else:
        effective_suspects = suspects or (
            fallback_suspects(plan, issue) if plan else []
        )
        if diff_only and read_line_range is not None:
            suspects_block = render_suspects_diff_only(effective_suspects, read_line_range)
        else:
            suspects_block, _ = render_suspects_with_snippets(effective_suspects, read_snippet)
        test_blocks = read_test_context(context, effective_suspects, plan)
        test_text = ""
        if test_blocks:
            test_text = "相关测试文件（补丁必须通过这些 assert）:\n" + "\n".join(test_blocks)

    allowed_files_line = ""
    if plan and plan.suspect_files:
        allowed_files_line = f"只允许修改以下文件: {', '.join(plan.suspect_files)}"

    extra_lines: list[str] = []
    if is_composite_multi_file(plan):
        assert plan is not None
        seen_paths = {s.file_path for s in effective_suspects if s.file_path}
        extra = [fp for fp in plan.suspect_files if fp not in seen_paths]
        if extra:
            extra_lines.append("其他相关源文件（代码已预读）:")
            for fp in extra:
                snippet = read_snippet(fp, 1, 80)
                if snippet:
                    extra_lines.append(f"  - {fp}")
                    extra_lines.append(snippet)

    render = render_skill_hint_for_plan(plan, "patcher") if plan else SkillBlockRender(
        text="", role="patcher", source="none"
    )
    if skill_render is not None:
        render = skill_render
    variables = build_patcher_variables(
        feedback=feedback,
        issue_hints_block="\n".join(build_issue_hints(plan, issue)),
        skill_hint_block=render.text,
        allowed_files_line=allowed_files_line,
        suspects_block=suspects_block,
        extra_files_block="\n".join(extra_lines),
        test_blocks=test_text,
    )
    return variables, render, subscribe_meta
