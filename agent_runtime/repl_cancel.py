"""REPL 协作式 cancel：/cancel 与 ask 期间二次 Ctrl+C。"""

from __future__ import annotations

import signal
import sys
from contextlib import contextmanager
from typing import Iterator

from agent_runtime.cancellation import CancellationToken

_active_session: ReplCancelSession | None = None


class ReplCancelSession:
    """ask() 期间安装 SIGINT：首次 cancel token，再次抛出 KeyboardInterrupt。"""

    def __init__(self, token: CancellationToken):
        self.token = token
        self._previous_handler = None

    def _handle_sigint(self, signum, frame) -> None:
        if not self.token.is_cancelled:
            self.token.cancel("user")
            print("\n[cancel] 正在取消当前任务…", file=sys.stderr)
            return
        raise KeyboardInterrupt

    def install(self) -> None:
        global _active_session
        _active_session = self
        self._previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_sigint)

    def restore(self) -> None:
        global _active_session
        if self._previous_handler is not None:
            signal.signal(signal.SIGINT, self._previous_handler)
        _active_session = None


def cancel_active_repl_task(reason: str = "user") -> bool:
    """对正在运行的 REPL ask 置位 cancel；无活动任务时返回 False。"""
    session = _active_session
    if session is None:
        return False
    if not session.token.is_cancelled:
        session.token.cancel(reason)
    return True


def has_active_repl_task() -> bool:
    return _active_session is not None


@contextmanager
def repl_cancel_scope(token: CancellationToken) -> Iterator[CancellationToken]:
    session = ReplCancelSession(token)
    session.install()
    try:
        yield token
    finally:
        session.restore()
