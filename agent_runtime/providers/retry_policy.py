"""HTTP 重试退避：Retry-After 解析与 jitter（stdlib only）。"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = [
    "RateLimitExceededError",
    "RetryPolicy",
    "apply_equal_jitter",
    "apply_full_jitter",
    "compute_rate_limit_delay",
    "compute_server_error_delay",
    "parse_retry_after",
]

DEFAULT_RETRY_CAP = 120.0
DEFAULT_RATE_LIMIT_BASE = 1.0
DEFAULT_SERVER_ERROR_BASE = 2.0
retry_context = ContextVar("model_retry_context", default=(None, None))


def wait_retry(delay: float, *, deadline: float | None = None, cancel_token=None) -> None:
    """Never dispatch before the minimum delay; fail if it cannot fit the deadline."""
    bound_token, bound_deadline = retry_context.get()
    token = cancel_token or bound_token
    deadlines = [value for value in (deadline, bound_deadline) if value is not None]
    end = time.monotonic() + max(0.0, delay)
    total_deadline = min(deadlines) if deadlines else None
    if token is not None:
        token.check()
    if total_deadline is not None and end >= total_deadline:
        raise TimeoutError("retry_deadline_exhausted: server wait exceeds remaining runtime")
    if token is None:
        time.sleep(max(0.0, delay))
    else:
        while time.monotonic() < end:
            token.check()
            time.sleep(min(0.05, end - time.monotonic()))
        token.check()
    if total_deadline is not None and time.monotonic() >= total_deadline:
        raise TimeoutError("retry_deadline_exhausted")


@dataclass(frozen=True)
class RetryPolicy:
    """Central retry decision for model/tool operations."""

    max_attempts: int = 1
    base_delay_s: float = 0.5
    max_delay_s: float = DEFAULT_RETRY_CAP
    idempotent: bool = True

    def should_retry(
        self,
        *,
        attempt: int,
        retryable: bool,
        deadline_s: float | None = None,
    ) -> bool:
        if not self.idempotent or not retryable or attempt >= max(1, self.max_attempts):
            return False
        return deadline_s is None or deadline_s > 0

    def delay(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        if retry_after_s is not None:
            return max(0.0, retry_after_s)
        else:
            raw = min(self.max_delay_s, self.base_delay_s * (2 ** max(0, attempt - 1)))
        return apply_equal_jitter(raw)


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
    """Server delay is a minimum, uncapped; otherwise exponential equal jitter."""
    if retry_after is not None:
        return max(0.0, retry_after)
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
