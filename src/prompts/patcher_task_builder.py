"""Patcher repair task 块组装（从 Orchestrator 抽出）。"""

from __future__ import annotations

from typing import Callable

from src.prompts.repair_tasks import build_patcher_variables
from src.repair.prompt_router import collect_patcher_user_hints, is_composite_multi_file
from src.skills.prompt import format_skill_hint
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
) -> dict[str, str]:
    effective_suspects = suspects or (
        fallback_suspects(plan, issue) if plan else []
    )

    allowed_files_line = ""
    if plan and plan.suspect_files:
        allowed_files_line = f"只允许修改以下文件: {', '.join(plan.suspect_files)}"

    suspects_lines: list[str] = []
    if effective_suspects:
        suspects_lines.append("嫌疑位置（代码已预读，无需再调用 read_file）:")
        for s in effective_suspects:
            if not s.file_path:
                continue
            suspects_lines.append(f"  - {s.file_path}:{s.start_line} ({s.reason})")
            snippet = read_snippet(s.file_path, s.start_line, s.end_line)
            if snippet:
                suspects_lines.append(snippet)
            else:
                suspects_lines.append(f"    ⚠ 文件不存在: {s.file_path}")

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

    test_blocks = read_test_context(context, effective_suspects, plan)
    test_text = ""
    if test_blocks:
        test_text = "相关测试文件（补丁必须通过这些 assert）:\n" + "\n".join(test_blocks)

    return build_patcher_variables(
        feedback=feedback,
        issue_hints_block="\n".join(build_issue_hints(plan, issue)),
        skill_hint_block=format_skill_hint(plan, "patcher"),
        allowed_files_line=allowed_files_line,
        suspects_block="\n".join(suspects_lines),
        extra_files_block="\n".join(extra_lines),
        test_blocks=test_text,
    )
