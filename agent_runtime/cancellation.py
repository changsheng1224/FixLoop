"""协作式取消：CancellationToken 供 L1 AgentLoop 与 L2 Orchestrator 共享。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Callable, TypeVar

T = TypeVar("T")

__all__ = [
    "CancelledError",
    "CancellationToken",
    "run_with_cancellation",
]

_DEFAULT_POLL_S = 0.05


class CancelledError(Exception):
    """CancellationToken.check() 在已取消时抛出。"""

    def __init__(self, reason: str = "user"):
        self.reason = reason
        super().__init__(reason)


class CancellationToken:
    """线程安全的协作式取消标志。"""

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._lock = threading.Lock()

    def cancel(self, reason: str = "user") -> None:
        with self._lock:
            self._cancelled = True
            self._reason = reason or "user"

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def check(self) -> None:
        if self.is_cancelled:
            raise CancelledError(self.reason or "user")


def run_with_cancellation(
    func: Callable[[], T],
    cancel_token: CancellationToken | None,
    *,
    poll_interval: float = _DEFAULT_POLL_S,
) -> T:
    """在后台线程执行 *func*，主线程轮询 cancel_token。"""
    if cancel_token is None:
        return func()
    if cancel_token.is_cancelled:
        cancel_token.check()

    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(func)
    try:
        while True:
            try:
                return fut.result(timeout=poll_interval)
            except FuturesTimeoutError:
                if cancel_token.is_cancelled:
                    cancel_token.check()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
