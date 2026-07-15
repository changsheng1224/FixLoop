"""HTTP 重试退避：Retry-After 解析与 jitter（stdlib only）。"""

from __future__ import annotations

import random
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = [
    "RateLimitExceededError",
    "apply_equal_jitter",
    "apply_full_jitter",
    "compute_rate_limit_delay",
    "compute_server_error_delay",
    "parse_retry_after",
]

DEFAULT_RETRY_CAP = 120.0
DEFAULT_RATE_LIMIT_BASE = 1.0
DEFAULT_SERVER_ERROR_BASE = 2.0


class RateLimitExceededError(RuntimeError):
    """429 重试耗尽；不计入 CircuitBreaker 连续失败。"""


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    """解析 Retry-After 头，返回建议等待秒数。"""
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        return max(0.0, (retry_at - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def apply_equal_jitter(delay: float) -> float:
    """Equal jitter：sleep ∈ [delay/2, delay]。"""
    if delay <= 0:
        return 0.0
    half = delay / 2.0
    return half + random.uniform(0.0, half)


def apply_full_jitter(cap: float) -> float:
    """Full jitter：sleep ∈ [0, cap]。"""
    if cap <= 0:
        return 0.0
    return random.uniform(0.0, cap)


def compute_rate_limit_delay(
    attempt: int,
    retry_after: float | None,
    *,
    base: float = DEFAULT_RATE_LIMIT_BASE,
    cap: float = DEFAULT_RETRY_CAP,
) -> float:
    """429 退避：优先 Retry-After，否则指数退避，再 equal jitter。"""
    if retry_after is not None:
        raw = min(cap, max(0.0, retry_after))
    else:
        raw = min(cap, base * (2**attempt))
    return apply_equal_jitter(raw)


def compute_server_error_delay(
    attempt: int,
    *,
    base: float = DEFAULT_SERVER_ERROR_BASE,
    cap: float = DEFAULT_RETRY_CAP,
) -> float:
    """5xx / 网络错误：指数上限 + full jitter。"""
    ceiling = min(cap, base * (2**attempt))
    return apply_full_jitter(ceiling)
