"""L1 REPL 输入：行尾 \\ 续行收集。"""

from __future__ import annotations

from typing import Callable

__all__ = [
    "line_ends_with_continuation",
    "read_repl_input",
    "strip_continuation_suffix",
]

Reader = Callable[[str], str]


def _normalize_line(line: str) -> str:
    """去掉 Windows 行尾 \\r。"""
    return line.rstrip("\r")


def _trailing_backslash_count(line: str) -> int:
    count = 0
    for ch in reversed(line):
        if ch == "\\":
            count += 1
        else:
            break
    return count


def line_ends_with_continuation(line: str) -> bool:
    """行尾奇数个反斜杠表示续行；偶数个（含 \\\\）为字面量。"""
    normalized = _normalize_line(line)
    return _trailing_backslash_count(normalized) % 2 == 1


def strip_continuation_suffix(line: str) -> str:
    """去掉表示续行的单个尾部反斜杠。"""
    normalized = _normalize_line(line)
    if line_ends_with_continuation(normalized):
        return normalized[:-1]
    return normalized


def read_repl_input(
    primary_prompt: str = "\n> ",
    continuation_prompt: str = "... ",
    *,
    reader: Reader = input,
) -> str:
    """读一行或多行（\\ 续行），返回拼接后的用户输入。"""
    first = _normalize_line(reader(primary_prompt))

    if first.lstrip().startswith("/"):
        return first

    parts: list[str] = []
    current = first

    while True:
        if line_ends_with_continuation(current):
            parts.append(strip_continuation_suffix(current))
            current = _normalize_line(reader(continuation_prompt))
            continue
        parts.append(current)
        break

    return "\n".join(parts)
