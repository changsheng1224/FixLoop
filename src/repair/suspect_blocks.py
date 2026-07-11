"""嫌疑位置 prompt 块渲染（Patcher / Degrade 共用）。"""

from __future__ import annotations

from typing import Callable

from src.state import SuspectLocation

__all__ = [
    "render_suspects_summary",
    "render_suspects_with_snippets",
]

_PATCHER_HEADER = "嫌疑位置（代码已预读，无需再调用 read_file）:"


def _sorted_suspects(suspects: list[SuspectLocation]) -> list[SuspectLocation]:
    return sorted(suspects, key=lambda s: (-s.confidence, s.file_path, s.start_line))


def render_suspects_with_snippets(
    suspects: list[SuspectLocation],
    read_snippet: Callable[[str, int, int], str],
    *,
    include_header: bool = True,
) -> tuple[str, list[SuspectLocation]]:
    """Patcher / Blackboard 订阅用的嫌疑块（含预读 snippet）。"""
    if not suspects:
        return "", []
    ordered = _sorted_suspects(suspects)
    lines = [_PATCHER_HEADER] if include_header else []
    for suspect in ordered:
        if not suspect.file_path:
            continue
        lines.append(f"  - {suspect.file_path}:{suspect.start_line} ({suspect.reason})")
        snippet = read_snippet(suspect.file_path, suspect.start_line, suspect.end_line)
        if snippet:
            lines.append(snippet)
        else:
            lines.append(f"    ⚠ 文件不存在: {suspect.file_path}")
    return "\n".join(lines), ordered


def render_suspects_summary(
    suspects: list[SuspectLocation],
    *,
    max_suspects: int = 8,
) -> str:
    """Degrade markdown 用的精简嫌疑列表（无 snippet）。"""
    if not suspects:
        return ""
    parts = []
    for suspect in suspects[:max_suspects]:
        parts.append(
            f"- {suspect.file_path}:{suspect.start_line}-{suspect.end_line} "
            f"({suspect.reason})"
        )
    return "\n".join(parts)
