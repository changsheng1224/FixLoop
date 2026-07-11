"""Circuit Breaker：API 熔断保护。

三态状态机：
  CLOSED（正常）→ 连续失败 ≥ threshold → OPEN（拒绝请求）
  → 等待 recovery_timeout 秒 → HALF_OPEN（探测）
  → 连续成功 ≥ half_open_success_threshold → CLOSED / 失败 → OPEN
"""

import time
from collections.abc import Callable
from enum import Enum

Listener = Callable[[str, dict], None]


class State(Enum):
    """熔断器三态：CLOSED / OPEN / HALF_OPEN。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """API 熔断器。

    当模型 API 连续失败达到阈值时自动熔断，
    避免在服务不可用时浪费资源和时间等待超时。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 2,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = max(1, half_open_success_threshold)
        self._state = State.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._opened_at = 0.0
        self._half_open_success_count = 0
        self._listeners: list[Listener] = []

    def add_listener(self, listener: Listener) -> None:
        """Register a trace/observability callback: ``listener(event, payload)``."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        """Remove a listener registered via :meth:`add_listener`."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _notify(self, event: str, **payload) -> None:
        if not self._listeners:
            return
        body = dict(payload)
        for listener in list(self._listeners):
            try:
                listener(event, body)
            except Exception:
                pass

    @property
    def state(self) -> str:
        """当前状态字符串（closed / open / half_open）。"""
        return self._state.value

    @property
    def half_open_success_count(self) -> int:
        """半开阶段已连续成功的 probe 次数。"""
        return self._half_open_success_count

    def _enter_half_open(self) -> None:
        self._state = State.HALF_OPEN
        self._half_open_success_count = 0
        self._notify(
            "half_open_probe",
            recovery_timeout_s=self.recovery_timeout,
            half_open_success_threshold=self.half_open_success_threshold,
        )

    def _open_circuit(self, *, reason: str, failure_count: int | None = None) -> None:
        self._state = State.OPEN
        self._opened_at = time.time()
        self._notify(
            "circuit_opened",
            reason=reason,
            failure_count=failure_count if failure_count is not None else self._failure_count,
            failure_threshold=self.failure_threshold,
            recovery_timeout_s=self.recovery_timeout,
        )

    def call(self, fn, *args, **kwargs):
        """包裹执行函数，熔断时直接拒绝。

        Args:
            fn: 要执行的函数（如 model_client.complete）。
            *args, **kwargs: 传递给 fn 的参数。

        Returns:
            fn 的返回值。

        Raises:
            CircuitBreakerOpenError: 熔断器打开时。
        """
        if self._state == State.OPEN:
            if time.time() - self._opened_at >= self.recovery_timeout:
                self._enter_half_open()
            else:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker is open. "
                    f"Retry in {self.recovery_timeout - (time.time() - self._opened_at):.0f}s"
                )

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            from agent_runtime.providers.retry_policy import RateLimitExceededError

            if isinstance(e, RateLimitExceededError):
                raise
            self._on_failure()
            raise e

    def _on_success(self):
        if self._state == State.HALF_OPEN:
            self._half_open_success_count += 1
            if self._half_open_success_count >= self.half_open_success_threshold:
                probes = self.half_open_success_threshold
                self._state = State.CLOSED
                self._half_open_success_count = 0
                self._notify(
                    "circuit_closed",
                    probes_required=probes,
                    probes_succeeded=probes,
                )
        self._failure_count = 0

    def _on_failure(self):
        if self._state == State.HALF_OPEN:
            self._half_open_success_count = 0
            self._open_circuit(reason="half_open_probe_failed", failure_count=self._failure_count)
            return

        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._open_circuit(reason="consecutive_failures")


class CircuitBreakerOpenError(Exception):
    """熔断器打开时抛出的异常。"""

    pass
