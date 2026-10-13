"""将 unified diff 应用到仓库文件（单文件 / 多文件）+ 等价性评分。"""

from __future__ import annotations

from pathlib import Path


def patch_equivalence(actual_diff: str, expected_diff: str) -> str:
    """比较 actual vs expected patch 的等价性。

    Returns:
        "full": 修改了相同文件且文件级内容一致
        "partial": 修改了相同文件但内容不完全一致
        "none": 没有共同的目标文件
    """
    actual_files = _target_files(actual_diff)
    expected_files = _target_files(expected_diff)
    if not actual_files or not expected_files:
        return "none"
    common = actual_files & expected_files
    if not common:
        return "none"
    if actual_files == expected_files:
        return "full"
    return "partial"


def _target_files(diff: str) -> set[str]:
    """提取 unified diff 中的目标文件路径（+++ b/... 行）。"""
    files: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.add(line[6:].strip())
    return files


def apply_unified_patch(repo: Path, patch_text: str) -> None:
    """Eval fixture adapter; the shared backend commits one complete transaction."""
    from agent_runtime.patch_engine import apply_file_patches, parse_unified_patch
    from agent_runtime.tool_context import ToolContext
    from agent_runtime.workspace_patches import HostPatchTransaction

    patches = parse_unified_patch(patch_text)
    context = ToolContext(root=str(repo))
    transaction = HostPatchTransaction(context.resolve, [patch.path for patch in patches])
    transaction.commit(apply_file_patches(transaction.sources(), patches))
