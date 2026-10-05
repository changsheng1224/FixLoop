"""AgentLoop 停机原因 canonical 枚举。"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CANONICAL_STOP_REASONS",
    "StopReason",
    "is_canonical_stop_reason",
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
    CONTEXT_BLOCKED = "context_blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    MODEL_OUTPUT_TRUNCATED = "model_output_truncated"


CANONICAL_STOP_REASONS = frozenset(member.value for member in StopReason)


def is_canonical_stop_reason(value: str) -> bool:
    return value in CANONICAL_STOP_REASONS
