"""阶段级读写锁（V1.4-Bonus15）。

localize/retrieve 共享读；patcher 独占写。
workspace 写窗口单飞：同一时刻最多一个 write phase。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Generator


class PhaseReadWriteLock:
    """阶段级读写锁。

    - 多个读 phase（localizer/retriever）可并发持有。
    - 写 phase（patcher）独占，等待所有读释放后获取。
    - 写 phase 持有期间，新读请求阻塞等待。

    Usage::

        lock = PhaseReadWriteLock()
        with lock.read():
            ...  # localizer / retriever
        with lock.write():
            ...  # patcher
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readers = 0
        self._write_active = False
        self._write_pending = threading.Condition(self._lock)

    @contextmanager
    def read(self) -> Generator[None, None, None]:
        """获取读锁（共享，多 reader 可并发）。"""
        with self._lock:
            self._readers += 1
        try:
            yield
        finally:
            with self._lock:
                self._readers -= 1
                if self._readers == 0:
                    self._write_pending.notify_all()

    @contextmanager
    def write(self) -> Generator[None, None, None]:
        """获取写锁（独占，等待所有 reader 释放）。"""
        with self._lock:
            while self._readers > 0:
                self._write_pending.wait()
            self._write_active = True
        try:
            yield
        finally:
            with self._lock:
                self._write_active = False
                self._write_pending.notify_all()

    @property
    def active_readers(self) -> int:
        with self._lock:
            return self._readers
