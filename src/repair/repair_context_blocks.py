"""Blackboard / RepairState → 共享 prompt 上下文块（Patcher + Degrade）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.blackboard import Blackboard
from src.repair.blackboard_merge import read_suspects_from_blackboard
from src.repair.blackboard_subscribe import render_patcher_prefix_blocks
from src.repair.suspect_blocks import render_suspects_summary, render_suspects_with_snippets
from src.state import RepairPlan, RepairState, RetrievedContext, SuspectLocation

__all__ = ["RepairContextBlocks", "build_repair_context_blocks"]


@dataclass
class RepairContextBlocks:
    suspects_block: str = ""
    test_blocks: str = ""
    scratch_block: str = ""
    suspects: list[SuspectLocation] | None = None
    from_blackboard: bool = False


def build_repair_context_blocks(
    state: RepairState,
    *,
    blackboard: Blackboard | None,
    read_snippet: Callable[[str, int, int], str],
    read_test_context: Callable[
        [RetrievedContext | None, list[SuspectLocation], RepairPlan | None],
        list[str],
    ],
    merge_for_patch: Callable[[RepairState], dict] | None = None,
    fallback_suspects: list[SuspectLocation] | None = None,
) -> RepairContextBlocks:
    """从 Blackboard（优先）或 RepairState 构建共享上下文块。"""
    if blackboard is not None:
        if merge_for_patch is not None:
            merge_for_patch(state)
        prefix_blocks = render_patcher_prefix_blocks(
            blackboard,
            read_snippet=read_snippet,
            read_test_context=read_test_context,
            plan=state.repair_plan,
        )
        suspects = read_suspects_from_blackboard(blackboard)
        if not suspects and fallback_suspects:
            suspects = list(fallback_suspects)
        return RepairContextBlocks(
            suspects_block=prefix_blocks.suspects_block,
            test_blocks=prefix_blocks.test_blocks,
            scratch_block=prefix_blocks.scratch_block,
            suspects=suspects,
            from_blackboard=True,
        )

    suspects = list(state.suspect_locations)
    if not suspects and fallback_suspects:
        suspects = list(fallback_suspects)
    suspects_block, _ = render_suspects_with_snippets(suspects, read_snippet)
    test_blocks_list = read_test_context(state.retrieved_context, suspects, state.repair_plan)
    test_blocks = ""
    if test_blocks_list:
        test_blocks = "相关测试文件（补丁必须通过这些 assert）:\n" + "\n".join(test_blocks_list)
    scratch_block = state.feedback.strip() if state.feedback.strip() else ""
    return RepairContextBlocks(
        suspects_block=suspects_block,
        test_blocks=test_blocks,
        scratch_block=scratch_block,
        suspects=suspects,
        from_blackboard=False,
    )


def append_degraded_context_sections(
    lines: list[str],
    blocks: RepairContextBlocks,
    *,
    state: RepairState,
) -> None:
    """将共享块以 Degrade markdown 章节追加到 *lines*。"""
    if blocks.suspects_block:
        lines.extend(["", "## 已定位嫌疑", blocks.suspects_block])
    elif blocks.suspects:
        summary = render_suspects_summary(blocks.suspects)
        if summary:
            lines.extend(["", "## 已定位嫌疑", summary])

    if blocks.test_blocks:
        lines.extend(["", "## 检索上下文", blocks.test_blocks])

    feedback = state.feedback.strip()
    if blocks.scratch_block and not feedback:
        lines.extend(["", "## 末轮验证反馈", blocks.scratch_block])
    elif feedback:
        lines.extend(["", "## 末轮验证反馈", feedback])
