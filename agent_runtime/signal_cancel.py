"""SIGINT 协作式 cancel：首次置位 token，再次抛出 KeyboardInterrupt。"""

from __future__ import annotations

import signal
import sys
from contextlib import contextmanager
from typing import Callable, Iterator

from agent_runtime.cancellation import CancellationToken

__all__ = [
    "install_sigint_cancel",
    "sigint_cancel_scope",
]


def install_sigint_cancel(
    token: CancellationToken,
    *,
    first_message: str = "[cancel] 正在取消当前任务…",
    on_first_cancel: Callable[[], None] | None = None,
):
    """安装 SIGINT handler；返回先前 handler 供 restore。"""

    def _handle_sigint(signum, frame) -> None:
        del signum, frame
        if not token.is_cancelled:
            token.cancel("user")
            if on_first_cancel is not None:
                on_first_cancel()
            elif first_message:
                print(f"\n{first_message}", file=sys.stderr)
            return
        raise KeyboardInterrupt

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)
    return previous


def restore_sigint(previous) -> None:
    if previous is not None:
        signal.signal(signal.SIGINT, previous)


@contextmanager
def sigint_cancel_scope(
    token: CancellationToken,
    *,
    first_message: str = "[cancel] 正在取消当前任务…",
    on_first_cancel: Callable[[], None] | None = None,
) -> Iterator[CancellationToken]:
    previous = install_sigint_cancel(
        token,
        first_message=first_message,
        on_first_cancel=on_first_cancel,
    )
    try:
        yield token
    finally:
        restore_sigint(previous)
