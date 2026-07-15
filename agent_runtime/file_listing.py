"""目录列举：depth 限制递归 + glob 过滤。"""

from __future__ import annotations

import fnmatch
from collections import deque
from pathlib import Path


def list_directory_entries(
    target: Path,
    *,
    depth: int = 1,
    glob_pattern: str = "",
    max_results: int = 200,
    ignored_names: frozenset[str],
) -> tuple[list[str], int]:
    """列举 target 下路径，返回 (输出行, 匹配总数)。"""
    depth = max(0, min(int(depth), 10))
    max_results = max(1, min(int(max_results), 500))
    pattern = (glob_pattern or "").strip()

    if depth == 1:
        return _list_shallow(target, pattern, max_results, ignored_names)
    return _list_recursive(target, depth, pattern, max_results, ignored_names)


def _list_shallow(
    target: Path,
    pattern: str,
    max_results: int,
    ignored_names: frozenset[str],
) -> tuple[list[str], int]:
    """depth=1：直接子项，文件与目录均输出。"""
    matched: list[str] = []
    total = 0
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        if _skip_name(child.name, ignored_names):
            continue
        if pattern and not _glob_matches(pattern, child.name, child.name):
            continue
        total += 1
        if len(matched) < max_results:
            prefix = "[D]" if child.is_dir() else "[F]"
            matched.append(f"{prefix} {child.name}")
    return matched, total


def _list_recursive(
    target: Path,
    depth: int,
    pattern: str,
    max_results: int,
    ignored_names: frozenset[str],
) -> tuple[list[str], int]:
    """depth>=2 或 depth=0：递归仅输出文件行（目录仅作遍历）。"""
    matched: list[str] = []
    total = 0
    queue: deque[tuple[Path, int]] = deque([(target, 0)])

    while queue:
        current, dir_depth = queue.popleft()
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            if _skip_name(child.name, ignored_names):
                continue
            try:
                rel = child.relative_to(target).as_posix()
            except ValueError:
                continue
            if _rel_has_ignored(rel, ignored_names):
                continue
            child_depth = dir_depth + 1
            if depth > 0 and child_depth > depth:
                continue
            if child.is_dir():
                if depth == 0 or child_depth < depth:
                    queue.append((child, child_depth))
                continue
            if pattern and not _glob_matches(pattern, rel, child.name):
                continue
            total += 1
            if len(matched) < max_results:
                matched.append(f"[F] {rel}")
            if len(matched) >= max_results and total > max_results:
                # 继续计数 total，但可提前结束遍历若只需截断提示
                pass
        if total > max_results * 2 and len(matched) >= max_results:
            # 足够估算截断，避免超大目录全量扫描
            break
    return matched, total


def _skip_name(name: str, ignored_names: frozenset[str]) -> bool:
    if name.startswith(".") and name not in {".", ".."}:
        return True
    return name in ignored_names


def _rel_has_ignored(rel_posix: str, ignored_names: frozenset[str]) -> bool:
    return any(part.startswith(".") or part in ignored_names for part in Path(rel_posix).parts)


def _glob_matches(pattern: str, rel_posix: str, basename: str) -> bool:
    return fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(basename, pattern)
