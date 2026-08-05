"""从磁盘快照 diff 提取 CandidatePatch（工具化编辑后的审计/导出）。"""

from __future__ import annotations

import difflib

from src.state import CandidatePatch

__all__ = ["patches_from_snapshot_diff", "unified_diff_for_path"]


def unified_diff_for_path(file_path: str, before: str, after: str) -> str:
    rel = (file_path or "unknown").replace("\\", "/").lstrip("./")
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    if old and not old[-1].endswith("\n"):
        old[-1] = old[-1] + "\n"
    if new and not new[-1].endswith("\n"):
        new[-1] = new[-1] + "\n"
    return "".join(
        difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}")
    )


def patches_from_snapshot_diff(
    before: dict[str, str],
    after: dict[str, str],
    *,
    explanation: str = "tool_edit",
) -> list[CandidatePatch]:
    """比较两份文本快照，生成已落盘修改对应的 CandidatePatch 列表。"""
    patches: list[CandidatePatch] = []
    paths = sorted(set(before) | set(after))
    for path in paths:
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is None:
            old = ""
        if new is None:
            # 删除：导出为空 patched；保留 pre-image
            new = ""
        diff = unified_diff_for_path(path, old, new)
        if not diff.strip():
            continue
        patches.append(
            CandidatePatch(
                file_path=path,
                original_lines=old,
                patched_lines=new,
                diff=diff,
                explanation=explanation,
            )
        )
    return patches
