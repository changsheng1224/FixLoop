"""AgentLoop 停机原因 canonical 枚举与 legacy 归一化。"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CANONICAL_STOP_REASONS",
    "RESERVED_STOP_REASONS",
    "StopReason",
    "is_canonical_stop_reason",
    "normalize_stop_reason",
    "stop_reason_detail_from_legacy",
]


class StopReason(StrEnum):
    """L1 ask() 终止原因（trace / report / task_state）。"""

    FINAL = "final"
    STEP_LIMIT = "step_limit"
    PARSE_FAIL = "parse_fail"
    CIRCUIT_BREAKER = "circuit_breaker"
    STEP_TIMEOUT = "step_timeout"
    RATE_LIMITED = "rate_limited"
    API_ERROR = "api_error"
    USER_CANCEL = "user_cancel"
    STALL = "stall"
    GOAL_DRIFT = "goal_drift"
    CONTEXT_OVERFLOW = "context_overflow"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"


CANONICAL_STOP_REASONS = frozenset(member.value for member in StopReason)

# 尚未接线的预留枚举 — normalize 时保持原值
RESERVED_STOP_REASONS: frozenset[str] = frozenset()


def is_canonical_stop_reason(value: str) -> bool:
    return value in CANONICAL_STOP_REASONS


def normalize_stop_reason(raw: str) -> str:
    """将 legacy 自由文本映射为 canonical stop_reason。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if text in CANONICAL_STOP_REASONS:
        return text
    lowered = text.lower()
    if lowered.startswith("tool_steps"):
        return StopReason.STEP_LIMIT.value
    if lowered.startswith("attempts"):
        return StopReason.PARSE_FAIL.value
    if lowered.startswith("error:"):
        return StopReason.API_ERROR.value
    return text


def stop_reason_detail_from_legacy(raw: str) -> str:
    """legacy 描述句在归一化后写入 stop_reason_detail。"""
    text = (raw or "").strip()
    if not text:
        return ""
    normalized = normalize_stop_reason(text)
    if normalized != text:
        return text
    return ""
