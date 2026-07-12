"""将 unified diff 应用到仓库文件（单文件 / 多文件）+ 等价性评分。"""

from __future__ import annotations

import re
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
    """在 repo 根目录应用 unified diff。"""
    for file_patch in _split_file_patches(patch_text):
        rel, hunks = file_patch
        path = repo / rel
        original = path.read_text(encoding="utf-8").splitlines(keepends=True)
        updated = _apply_hunks(original, hunks)
        path.write_text("".join(updated), encoding="utf-8")


def _split_file_patches(patch_text: str) -> list[tuple[str, list[str]]]:
    chunks = re.split(r"(?=^--- a/)", patch_text.strip(), flags=re.MULTILINE)
    results: list[tuple[str, list[str]]] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        plus = next((ln[6:] for ln in lines if ln.startswith("+++ b/")), None)
        if not plus:
            continue
        hunks = [ln for ln in lines if ln.startswith("@@") or ln[:1] in " +-"]
        results.append((plus, hunks))
    return results


def _apply_hunks(original: list[str], hunk_lines: list[str]) -> list[str]:
    lines = original[:]
    i = 0
    while i < len(hunk_lines):
        line = hunk_lines[i]
        if not line.startswith("@@"):
            i += 1
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if not m:
            i += 1
            continue
        old_start = int(m.group(1)) - 1
        i += 1
        old_idx = old_start
        new_segment: list[str] = []
        while i < len(hunk_lines) and not hunk_lines[i].startswith("@@"):
            hl = hunk_lines[i]
            if hl.startswith(" "):
                new_segment.append(lines[old_idx])
                old_idx += 1
            elif hl.startswith("-"):
                old_idx += 1
            elif hl.startswith("+"):
                text = hl[1:]
                new_segment.append(text if text.endswith("\n") else text + "\n")
            i += 1
        lines[old_start:old_idx] = new_segment
    return lines
