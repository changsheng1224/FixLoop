"""Two fixed read operations at most; all other work stays on the owner thread."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .workspace import snapshot


class ReadBudget:
    def __init__(self, limit: int = 4, deadline: float | None = None):
        self.limit = limit
        self.used = 0
        self.deadline = deadline
        self.lock = threading.Lock()

    def reserve(self, count: int):
        with self.lock:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ValueError("read_deadline_exceeded")
            if self.used + count > self.limit:
                raise ValueError("read_budget_exceeded")
            self.used += count


class PlanScheduler:
    def __init__(self, session, budget: ReadBudget | None = None):
        self.session = session
        self.budget = budget or ReadBudget()

    def run_reads(self, callbacks: dict) -> list[str]:
        session = self.session
        session.refresh()
        if any(n.status in {"running", "uncertain"} for n in session.plan.nodes):
            raise ValueError("parallel_reads_conflict")
        ready = [n for n in session.plan.nodes if n.status == "ready" and n.kind == "explore"][:2]
        if not ready:
            return []
        self.budget.reserve(len(ready))
        before = snapshot(session.workspace)
        attempts = [(n, session.prepare(n.node_id)) for n in ready]
        session.emit("parallel_reads_started", nodes=[n.node_id for n in ready])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                (n, a, pool.submit(session.invoke, a, callbacks[n.node_id])) for n, a in attempts
            ]
            recorded = []
            for node, attempt, future in futures:
                try:
                    recorded.append(future.result())
                except Exception as exc:
                    recorded.append(
                        session.record_result(
                            attempt,
                            {
                                "status": "uncertain",
                                "reason": str(exc),
                            },
                        )
                    )
        if before != snapshot(session.workspace):
            recorded = [
                session.record_result(
                    a, {"status": "uncertain", "reason": "external_workspace_change"}
                )
                for a in recorded
            ]
        for attempt in recorded:
            session.settle(attempt)
        return [n.node_id for n in ready]
