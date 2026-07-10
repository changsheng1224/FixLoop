"""Gate 9 工具执行超时：ThreadPoolExecutor + result(timeout)。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Callable, TypeVar

T = TypeVar("T")

__all__ = ["ToolTimeoutError", "run_with_timeout"]


class ToolTimeoutError(Exception):
    """工具 run() 超过配置秒数仍未返回。"""

    def __init__(self, timeout_s: int):
        self.timeout_s = int(timeout_s)
        super().__init__(f"tool execution timed out after {self.timeout_s}s")


def run_with_timeout(func: Callable[[], T], *, timeout_s: int) -> T:
    """在独立线程中执行 *func*；*timeout_s*≤0 时不限时。"""
    if timeout_s <= 0:
        return func()

    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(func)
    try:
        return fut.result(timeout=timeout_s)
    except FuturesTimeoutError as exc:
        raise ToolTimeoutError(timeout_s) from exc
    finally:
        # 不在超时后等待孤儿线程结束（with 块默认 wait=True 会阻塞到 run() 返回）
        pool.shutdown(wait=False, cancel_futures=True)
