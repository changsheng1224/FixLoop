"""Workspace/run read capacity shared by Plan exploration and native batches."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path


class ReadPermitPool:
    def __init__(self):
        self._slots = threading.BoundedSemaphore(2)
        self._local = threading.local()

    @property
    def inherited(self) -> bool:
        return bool(getattr(self._local, "held", False))

    def acquire(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self):
        self._slots.release()

    def borrow(self):
        """Keep a parent's capacity leased until its nested worker exits."""
        lease = self._local.lease
        with lease["lock"]:
            lease["references"] += 1

        def release():
            with lease["lock"]:
                lease["references"] -= 1
                if not lease["references"]:
                    self.release()

        return release

    @contextmanager
    def lease(self, token=None):
        """A nested batch borrows its parent's permit and runs one call at a time."""
        import time

        if self.inherited:
            yield
            return
        while not self.acquire():
            if token is not None:
                token.check()
            time.sleep(0.01)
        self._local.held = True
        lease = {"lock": threading.Lock(), "references": 1}
        self._local.lease = lease
        try:
            yield
        finally:
            self._local.held = False
            with lease["lock"]:
                lease["references"] -= 1
                if not lease["references"]:
                    self.release()


_pools: dict[tuple[str, str], ReadPermitPool] = {}
_lock = threading.Lock()


def read_permits(workspace: str, run_id: str) -> ReadPermitPool:
    key = (str(Path(workspace).resolve()).casefold(), str(run_id))
    with _lock:
        return _pools.setdefault(key, ReadPermitPool())
