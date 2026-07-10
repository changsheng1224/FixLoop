"""Patcher repair task 块组装（从 Orchestrator 抽出）。"""

from __future__ import annotations

import re
from typing import Callable

from src.prompts.repair_tasks import build_patcher_variables
from src.state import RepairPlan, RetrievedContext, SuspectLocation

__all__ = ["assemble_patcher_variables", "build_issue_hints"]


def build_issue_hints(plan: RepairPlan | None, issue: str) -> list[str]:
    hints: list[str] = []
    if plan and re.search(r"cannot import name", issue, re.IGNORECASE):
        hints.append(
            "修复提示: 除 import 行外，须同步修改本文件内对错误符号名的所有调用。"
        )
    if plan and plan.issue_type == "composite":
        hints.append(
            f"至少修改 {len(plan.suspect_files or [])} 个相关文件中的每一处错误。"
        )
    if issue and "concatenate str" in issue.lower():
        hints.append(
            "Issue 表明 str 与 int 不能直接相加；修复后混合类型输入应得到数字运算结果。"
        )
    return hints


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
    if plan and plan.issue_type == "composite" and plan.suspect_files:
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
        allowed_files_line=allowed_files_line,
        suspects_block="\n".join(suspects_lines),
        extra_files_block="\n".join(extra_lines),
        test_blocks=test_text,
    )
