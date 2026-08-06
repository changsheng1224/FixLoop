"""嫌疑位置 prompt 块渲染（Patcher / Degrade 共用）。"""

from __future__ import annotations

from collections.abc import Callable

from src.state import SuspectLocation

__all__ = [
    "render_suspects_diff_only",
    "render_suspects_summary",
    "render_suspects_with_snippets",
]

# diff-only 默认上下文行数
DIFF_CONTEXT_LINES = 2

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
            f"- {suspect.file_path}:{suspect.start_line}-{suspect.end_line} ({suspect.reason})"
        )
    return "\n".join(parts)


def render_suspects_diff_only(
    suspects: list[SuspectLocation],
    read_line_range: Callable[[str, int, int], str],
    *,
    context_lines: int = DIFF_CONTEXT_LINES,
    max_suspects: int = 6,
) -> str:
    """Unified diff 格式的嫌疑上下文（token 高效，省去 markdown 包装）。

    每嫌疑位置输出标准 unified diff header + suspect 行 ± context_lines 邻域。
    相对整文件注入，token 上界大幅下降，同时保留 hunk 头供模型定位。
    """
    if not suspects:
        return ""
    ordered = _sorted_suspects(suspects)
    blocks: list[str] = []
    for suspect in ordered[:max_suspects]:
        if not suspect.file_path:
            continue
        start = max(1, suspect.start_line - context_lines)
        end = suspect.end_line + context_lines
        code = read_line_range(suspect.file_path, start, end)
        if not code:
            blocks.append(
                f"--- a/{suspect.file_path}\n"
                f"+++ b/{suspect.file_path}\n"
                f"@@ -{suspect.start_line},{suspect.end_line - suspect.start_line + 1}"
                f" +{suspect.start_line},{suspect.end_line - suspect.start_line + 1} @@"
                f" ({suspect.reason})\n"
                f"  ⚠ 文件不可读"
            )
            continue

        lines = code.split("\n")
        suspect_set = set(range(suspect.start_line, suspect.end_line + 1))
        # 构建 unified diff 格式
        hunk_lines = [
            f"--- a/{suspect.file_path}",
            f"+++ b/{suspect.file_path}",
            f"@@ -{start},{len(lines)} +{start},{len(lines)} @@ ({suspect.reason})",
        ]
        for i, line_text in enumerate(lines):
            line_no = start + i
            if line_no in suspect_set:
                hunk_lines.append(f"-{line_text}")  # 嫌疑行
            else:
                hunk_lines.append(f" {line_text}")  # 上下文行
        blocks.append("\n".join(hunk_lines))

    return "\n".join(blocks)
