"""磁盘真源片段：供 Patcher 从中一字不差复制 original_lines。"""

from __future__ import annotations

from collections.abc import Callable

from src.state import SuspectLocation

__all__ = ["build_disk_grounding_block", "collect_grounding_targets"]

DEFAULT_CONTEXT_LINES = 8
MAX_GROUNDING_FILES = 4
MAX_GROUNDING_CHARS = 8000


def collect_grounding_targets(
    suspects: list[SuspectLocation],
    *,
    plan_files: list[str] | None = None,
    max_files: int = MAX_GROUNDING_FILES,
) -> list[tuple[str, int, int]]:
    """收集 (path, start_line, end_line)，按文件合并窗口。"""
    windows: dict[str, list[tuple[int, int]]] = {}
    for s in suspects:
        if not s.file_path:
            continue
        windows.setdefault(s.file_path, []).append((s.start_line, s.end_line))
    for fp in plan_files or []:
        if fp and fp not in windows:
            windows[fp] = [(1, 80)]

    ordered_paths = list(windows.keys())[:max_files]
    out: list[tuple[str, int, int]] = []
    for path in ordered_paths:
        spans = windows[path]
        start = min(a for a, _ in spans)
        end = max(b for _, b in spans)
        out.append((path, start, end))
    return out


def build_disk_grounding_block(
    targets: list[tuple[str, int, int]],
    read_line_range: Callable[[str, int, int], str],
    *,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    max_chars: int = MAX_GROUNDING_CHARS,
) -> str:
    """渲染带行号的 verbatim 源码块（无 markdown/diff 前缀，避免污染 original_lines）。"""
    if not targets:
        return ""

    parts: list[str] = [
        "DISK GROUNDING（original_lines 必须从下列原文一字不差复制，含缩进；不要复制行号前缀）:",
    ]
    used = 0
    for path, start_line, end_line in targets:
        start = max(1, start_line - context_lines)
        end = end_line + context_lines
        code = read_line_range(path, start, end)
        if not code:
            parts.append(f"### {path}\n  (unreadable)")
            continue
        lines = code.split("\n")
        body_lines = [f"{start + i}|{line}" for i, line in enumerate(lines)]
        body = "\n".join(body_lines)
        chunk = f"### {path}\n{body}"
        if used + len(chunk) > max_chars and used > 0:
            parts.append(f"### {path}\n  (truncated for budget)")
            break
        if len(chunk) > max_chars:
            chunk = chunk[: max_chars - 20] + "\n  ...(truncated)"
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)
