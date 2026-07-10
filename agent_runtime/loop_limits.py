"""AgentLoop 步数 / 解析尝试上限常量。"""

from __future__ import annotations

__all__ = [
    "NATIVE_MAX_TURNS_MESSAGE",
    "PARSE_ATTEMPT_BASE",
    "PARSE_ATTEMPTS_PER_STEP",
    "max_parse_attempts",
]

PARSE_ATTEMPTS_PER_STEP = 3
PARSE_ATTEMPT_BASE = 4
NATIVE_MAX_TURNS_MESSAGE = "max_turns exceeded"


def max_parse_attempts(max_steps: int) -> int:
    """XML 路径：格式错误重试总 attempt 上限。"""
    return int(max_steps) * PARSE_ATTEMPTS_PER_STEP + PARSE_ATTEMPT_BASE
