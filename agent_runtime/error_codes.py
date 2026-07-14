"""FixLoop 统一错误码枚举 — L1/L2 emit 统一 error_code 字段。"""

from __future__ import annotations

from enum import StrEnum


class FixLoopErrorCode(StrEnum):
    """FixLoop 全链路错误码（gate / phase / semantic / context / budget）。"""

    # 工具闸口 (Gate)
    GATE1_NOT_ALLOWED = "GATE1_NOT_ALLOWED"
    GATE2_NOT_REGISTERED = "GATE2_NOT_REGISTERED"
    GATE3_PARAM_INVALID = "GATE3_PARAM_INVALID"
    GATE4_PATH_ESCAPE = "GATE4_PATH_ESCAPE"
    GATE5_DUPLICATE = "GATE5_DUPLICATE"
    GATE6_APPROVAL_DENIED = "GATE6_APPROVAL_DENIED"
    GATE7_QUOTA_EXCEEDED = "GATE7_QUOTA_EXCEEDED"

    # 阶段超时 (Phase)
    PHASE_TIMEOUT = "PHASE_TIMEOUT"

    # 语义漂移 (Semantic)
    SEMANTIC_DRIFT = "SEMANTIC_DRIFT"

    # 上下文溢出 (Context)
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"

    # 预算耗尽 (Budget)
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

    # API 错误 (API)
    API_ERROR = "API_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"

    # 用户交互
    USER_CANCEL = "USER_CANCEL"

    # 停滞/漂移
    STALL = "STALL"
    GOAL_DRIFT = "GOAL_DRIFT"


# StopReason → FixLoopErrorCode 映射
STOP_REASON_TO_ERROR_CODE: dict[str, FixLoopErrorCode] = {
    "step_limit": FixLoopErrorCode.BUDGET_EXHAUSTED,
    "parse_fail": FixLoopErrorCode.API_ERROR,
    "circuit_breaker": FixLoopErrorCode.CIRCUIT_BREAKER,
    "step_timeout": FixLoopErrorCode.PHASE_TIMEOUT,
    "rate_limited": FixLoopErrorCode.RATE_LIMITED,
    "api_error": FixLoopErrorCode.API_ERROR,
    "user_cancel": FixLoopErrorCode.USER_CANCEL,
    "stall": FixLoopErrorCode.STALL,
    "goal_drift": FixLoopErrorCode.GOAL_DRIFT,
    "context_overflow": FixLoopErrorCode.CONTEXT_OVERFLOW,
    "budget_exhausted": FixLoopErrorCode.BUDGET_EXHAUSTED,
}


__all__ = ["FixLoopErrorCode", "STOP_REASON_TO_ERROR_CODE"]
