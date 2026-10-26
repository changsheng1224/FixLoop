"""D2 experiment: public graph APIs with callbacks on the existing owner thread.

This is probe infrastructure, not a second scheduler or the production repair path.
The graph selects stages; this bridge only transfers synchronous stage calls back
to the caller, preserving PlanSession ownership and the caller's ContextVars.
"""

from concurrent.futures import Future
from contextvars import copy_context
from queue import Queue
from threading import Lock, Thread, get_ident


class OwnerCalls:
    def __init__(self):
        self.owner_thread = get_ident()
        self._queue = Queue()
        self._lock = Lock()
        self._active = False

    def call(self, callback):
        with self._lock:
            if not self._active:
                raise RuntimeError("owner_bridge_closed")
            if get_ident() != self.owner_thread:
                result = Future()
                self._queue.put((callback, result))
            else:
                result = None
        return callback() if result is None else result.result()

    def run(self, operation):
        if get_ident() != self.owner_thread:
            raise RuntimeError("owner_thread_required")
        with self._lock:
            if self._active:
                raise RuntimeError("owner_bridge_already_running")
            self._active = True
        outcome = Future()
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

        worker = Thread(target=lambda: context.run(work), name="langgraph-d2", daemon=False)
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
            callback, result = request
            if interrupted is not None:
                result.set_exception(interrupted)
                continue
            try:
                result.set_result(callback())
            except BaseException as exc:
                result.set_exception(exc)
        worker.join()
        if interrupted is not None:
            raise interrupted
        return outcome.result()
