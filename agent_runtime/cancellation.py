"""协作式取消：CancellationToken 供 L1 AgentLoop 与 L2 Orchestrator 共享。"""

from __future__ import annotations

import threading

__all__ = ["CancelledError", "CancellationToken"]


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
