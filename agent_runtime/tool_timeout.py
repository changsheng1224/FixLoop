"""Gate 9 工具执行超时：基于 cancellation.run_blocking。"""

from __future__ import annotations

from collections.abc import Callable
import multiprocessing as mp
import queue
import time
from typing import TypeVar

from agent_runtime.cancellation import BlockingDeadlineError, CancellationToken, run_blocking

T = TypeVar("T")

__all__ = ["ToolCancelledError", "ToolIsolationError", "ToolTimeoutError", "run_with_timeout"]


class ToolIsolationError(RuntimeError):
    """The requested process-isolated execution could not be started."""


class ToolTimeoutError(Exception):
    """工具 run() 超过配置秒数仍未返回。"""

    def __init__(self, timeout_s: int, *, termination_guaranteed: bool = False):
        self.timeout_s = int(timeout_s)
        self.termination_guaranteed = termination_guaranteed
        super().__init__(f"tool execution timed out after {self.timeout_s}s")


class ToolCancelledError(ToolTimeoutError):
    """Process-isolated tool was terminated due to cancellation."""

    def __init__(self):
        super().__init__(0, termination_guaranteed=True)
        self.cancelled = True


def run_with_timeout(
    func: Callable[[], T],
    *,
    timeout_s: int,
    cancel_token: CancellationToken | None = None,
    mode: str = "thread",
) -> T:
    """Execute a tool with cooperative thread or hard process isolation."""
    if mode == "process":
        return _run_process_isolated(func, timeout_s=timeout_s, cancel_token=cancel_token)
    if mode not in {"thread", "cooperative"}:
        raise ValueError(f"unknown tool execution mode: {mode}")
    if timeout_s <= 0 and cancel_token is None:
        return func()
    try:
        return run_blocking(func, cancel_token=cancel_token, timeout_s=timeout_s or None)
    except BlockingDeadlineError as exc:
        raise ToolTimeoutError(int(exc.timeout_s)) from exc


def _process_entry(func, output_queue) -> None:
    try:
        output_queue.put(("ok", func()))
    except BaseException as exc:  # pragma: no cover - executed in child process
        output_queue.put(("error", type(exc).__name__, str(exc)))


def _run_process_isolated(func: Callable[[], T], *, timeout_s: int, cancel_token=None) -> T:
    """Run a pickleable callable in a killable child process."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=1)
    process = context.Process(target=_process_entry, args=(func, output_queue), daemon=True)
    try:
        process.start()
    except (OSError, TypeError, AttributeError) as exc:
        raise ToolIsolationError(f"process isolation unavailable: {exc}") from exc
    deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
    try:
        while process.is_alive():
            if cancel_token is not None and cancel_token.is_cancelled:
                process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
                raise ToolCancelledError()
            if deadline is not None and time.monotonic() >= deadline:
                process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
                raise ToolTimeoutError(timeout_s, termination_guaranteed=True)
            time.sleep(0.02)
        try:
            payload = output_queue.get_nowait()
        except queue.Empty as exc:
            raise ToolIsolationError("isolated tool exited without a result") from exc
        if payload[0] == "error":
            raise RuntimeError(f"{payload[1]}: {payload[2]}")
        return payload[1]
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
