"""Per-tool rate limiting, circuit breaking and retry policy."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ResilienceDecision:
    allowed: bool
    reason: str = ""
    retry_after_s: float = 0.0


class _Bucket:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.failures = 0
        self.open_until = 0.0


class ToolResilienceController:
    """Thread-safe, low-cardinality resilience state scoped to one Agent run."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._buckets: dict[str, _Bucket] = {}

    def before(self, name: str, spec: dict) -> ResilienceDecision:
        rate = max(0, int(spec.get("rate_limit_per_minute", 0) or 0))
        threshold = max(0, int(spec.get("circuit_breaker_threshold", 0) or 0))
        if rate <= 0 and threshold <= 0:
            return ResilienceDecision(True)
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.setdefault(name, _Bucket())
            if bucket.open_until > now:
                return ResilienceDecision(
                    False, "circuit_open", max(0.0, bucket.open_until - now)
                )
            if rate > 0:
                bucket.calls = [stamp for stamp in bucket.calls if now - stamp < 60.0]
                if len(bucket.calls) >= rate:
                    return ResilienceDecision(False, "rate_limited", 60.0 - (now - bucket.calls[0]))
                bucket.calls.append(now)
        return ResilienceDecision(True)

    def after(self, name: str, spec: dict, *, success: bool) -> None:
        threshold = max(0, int(spec.get("circuit_breaker_threshold", 0) or 0))
        if threshold <= 0:
            return
        with self._lock:
            bucket = self._buckets.setdefault(name, _Bucket())
            if success:
                bucket.failures = 0
                bucket.open_until = 0.0
                return
            bucket.failures += 1
            if bucket.failures >= threshold:
                bucket.open_until = time.monotonic() + min(60.0, 2.0 ** min(bucket.failures, 6))

    @staticmethod
    def max_attempts(spec: dict) -> int:
        if str(spec.get("replay_policy", "revalidate")) == "never_replay":
            return 1
        return max(1, min(5, int(spec.get("max_retries", 0) or 0) + 1))

    @staticmethod
    def backoff_s(spec: dict, attempt: int) -> float:
        base = max(0.0, float(spec.get("retry_backoff_s", 0.1) or 0.1))
        return min(30.0, base * (2 ** max(0, attempt - 1)))


__all__ = ["ResilienceDecision", "ToolResilienceController"]
