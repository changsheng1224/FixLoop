"""REPL 协作式 cancel：/cancel 与 ask 期间二次 Ctrl+C。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from agent_runtime.cancellation import CancellationToken
from agent_runtime.signal_cancel import sigint_cancel_scope

_active_token: CancellationToken | None = None


def cancel_active_repl_task(reason: str = "user") -> bool:
    """对正在运行的 REPL ask 置位 cancel；无活动任务时返回 False。"""
    if _active_token is None:
        return False
    if not _active_token.is_cancelled:
        _active_token.cancel(reason)
    return True


def has_active_repl_task() -> bool:
    return _active_token is not None


@contextmanager
def repl_cancel_scope(token: CancellationToken) -> Iterator[CancellationToken]:
    global _active_token
    _active_token = token
    with sigint_cancel_scope(token):
        try:
            yield token
        finally:
            _active_token = None
