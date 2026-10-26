"""Return graph stage calls to the thread that owns the execution session."""

from collections.abc import Callable
from concurrent.futures import Future
from contextvars import copy_context
from dataclasses import dataclass
from queue import Queue
from threading import Lock, Thread, get_ident
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class _OwnerRequest:
    callback: Callable[[], Any]
    result: Future


class OwnerCalls:
    """Transfer calls, never policy, state ownership or resource cleanup.

    The caller pumps requests until graph execution stops. A pending stage is
    never abandoned on a short wait timeout; existing cancellation/deadlines
    remain responsible for stopping and reconciling that stage.
    """

    def __init__(self):
        self.owner_thread = get_ident()
        self._queue: Queue[_OwnerRequest | None] = Queue()
        self._lock = Lock()
        self._active = False

    def call(self, callback: Callable[[], T]) -> T:
        with self._lock:
            if not self._active:
                raise RuntimeError("owner_bridge_closed")
            if get_ident() != self.owner_thread:
                result = Future()
                self._queue.put(_OwnerRequest(callback, result))
            else:
                result = None
        return callback() if result is None else result.result()

    def run(self, operation: Callable[[], T]) -> T:
        if get_ident() != self.owner_thread:
            raise RuntimeError("owner_thread_required")
        with self._lock:
            if self._active:
                raise RuntimeError("owner_bridge_already_running")
            self._active = True
        outcome: Future[T] = Future()
        context = copy_context()

        def work():
            try:
                outcome.set_result(operation())
            except BaseException as exc:
                outcome.set_exception(exc)
            finally:
                with self._lock:
                    self._active = False
                self._queue.put(None)

        worker = Thread(target=lambda: context.run(work), name="fixloop-repair-graph", daemon=False)
        try:
            worker.start()
        except BaseException:
            with self._lock:
                self._active = False
            raise
        interrupted = None
        while True:
            try:
                request = self._queue.get()
            except BaseException as exc:
                interrupted = interrupted or exc
                continue
            if request is None:
                break
            if interrupted is not None:
                request.result.set_exception(interrupted)
                continue
            try:
                request.result.set_result(request.callback())
            except BaseException as exc:
                request.result.set_exception(exc)
        worker.join()
        if interrupted is not None:
            raise interrupted
        return outcome.result()
