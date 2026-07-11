"""协作式取消：CancellationToken 供 L1 AgentLoop 与 L2 Orchestrator 共享。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Callable, TypeVar

T = TypeVar("T")

__all__ = [
    "BlockingDeadlineError",
    "CancelledError",
    "CancellationToken",
    "run_blocking",
    "run_with_cancellation",
    "wait_future",
]

_DEFAULT_POLL_S = 0.05


class CancelledError(Exception):
    """协作式取消；*answer* 非空时表示 AgentLoop 应直接返回该 final 文本。"""

    def __init__(self, reason: str = "user", *, answer: str = ""):
        self.reason = reason
        self.answer = answer
        super().__init__(reason)


class BlockingDeadlineError(Exception):
    """run_blocking / wait_future 达到 wall-clock 上限。"""

    def __init__(self, timeout_s: float):
        self.timeout_s = float(timeout_s)
        super().__init__(f"blocking operation timed out after {self.timeout_s}s")


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


def wait_future(
    fut: Future[T],
    *,
    poll_interval: float = _DEFAULT_POLL_S,
    cancel_token: CancellationToken | None = None,
    deadline: float | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> T:
    """轮询 *fut*，支持 cancel_token 与绝对 deadline（monotonic 秒时间戳）。"""
    if cancel_token is not None and cancel_token.is_cancelled:
        cancel_token.check()

    while True:
        remaining = None if deadline is None else max(0.01, deadline - time.time())
        wait_s = poll_interval if remaining is None else min(poll_interval, remaining)
        try:
            return fut.result(timeout=wait_s)
        except FuturesTimeoutError:
            if cancel_token is not None and cancel_token.is_cancelled:
                if on_cancel is not None:
                    on_cancel()
                cancel_token.check()
            if deadline is not None and time.time() >= deadline:
                if on_cancel is not None:
                    on_cancel()
                raise BlockingDeadlineError(0)


def run_blocking(
    func: Callable[[], T],
    *,
    cancel_token: CancellationToken | None = None,
    timeout_s: float | None = None,
    poll_interval: float = _DEFAULT_POLL_S,
    on_cancel: Callable[[], None] | None = None,
) -> T:
    """在后台线程执行 *func*，主线程轮询 cancel / deadline。"""
    if cancel_token is not None and cancel_token.is_cancelled:
        cancel_token.check()
    if timeout_s is not None and timeout_s <= 0:
        timeout_s = None
    if cancel_token is None and timeout_s is None:
        return func()

    deadline = time.time() + timeout_s if timeout_s is not None else None
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(func)
    try:
        return wait_future(
            fut,
            poll_interval=poll_interval,
            cancel_token=cancel_token,
            deadline=deadline,
            on_cancel=on_cancel,
        )
    except BlockingDeadlineError as exc:
        if timeout_s is not None:
            raise BlockingDeadlineError(timeout_s) from exc
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def run_with_cancellation(
    func: Callable[[], T],
    cancel_token: CancellationToken | None,
    *,
    poll_interval: float = _DEFAULT_POLL_S,
) -> T:
    """在后台线程执行 *func*，主线程轮询 cancel_token。"""
    return run_blocking(func, cancel_token=cancel_token, poll_interval=poll_interval)
