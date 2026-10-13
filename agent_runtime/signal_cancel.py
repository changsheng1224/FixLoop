"""SIGINT 协作式 cancel：首次置位 token，再次抛出 KeyboardInterrupt。"""

from __future__ import annotations

import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

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
            # Mark only. The durable cancel chain runs on the owner thread from
            # ``sigint_cancel_scope``'s exit: in the interrupted frame it would
            # block on a lock that same frame holds and could raise at any
            # bytecode. Immediate feedback stays here because it is cheap.
            token.request("user")
            if first_message:
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
    previous = install_sigint_cancel(token, first_message=first_message)
    try:
        yield token
    finally:
        try:
            # The handler only marked the token; run the durable chain here,
            # once the interrupted work has unwound and this thread owns a
            # defined point again.
            if token.is_cancelled:
                if on_first_cancel is not None:
                    on_first_cancel()
                token.run_pending_callbacks()
        except Exception as exc:  # noqa: BLE001 - must not mask the body's error
            from agent_runtime.logging_setup import get_logger

            get_logger("signal_cancel").warning("取消回调执行失败: %s", exc)
        finally:
            restore_sigint(previous)
