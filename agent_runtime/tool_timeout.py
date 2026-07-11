"""Gate 9 工具执行超时：基于 cancellation.run_blocking。"""

from __future__ import annotations

from typing import Callable, TypeVar

from agent_runtime.cancellation import BlockingDeadlineError, CancellationToken, run_blocking

T = TypeVar("T")

__all__ = ["ToolTimeoutError", "run_with_timeout"]


class ToolTimeoutError(Exception):
    """工具 run() 超过配置秒数仍未返回。"""

    def __init__(self, timeout_s: int):
        self.timeout_s = int(timeout_s)
        super().__init__(f"tool execution timed out after {self.timeout_s}s")


def run_with_timeout(
    func: Callable[[], T],
    *,
    timeout_s: int,
    cancel_token: CancellationToken | None = None,
) -> T:
    """在独立线程中执行 *func*；*timeout_s*≤0 时不限时；可选协作式 cancel。"""
    if timeout_s <= 0 and cancel_token is None:
        return func()
    try:
        return run_blocking(func, cancel_token=cancel_token, timeout_s=timeout_s or None)
    except BlockingDeadlineError as exc:
        raise ToolTimeoutError(int(exc.timeout_s)) from exc
