"""Small durable scheduler adapter for TaskDAG and CollaborationStore."""

from __future__ import annotations

from collections.abc import Callable

from src.collaboration.budget import BudgetLedger
from src.collaboration.contracts import AgentResult, AgentTask, TaskStatus
from src.collaboration.dag import TaskDAG
from src.collaboration.store import CollaborationStore


class TaskScheduler:
    def __init__(self, store: CollaborationStore, *, budget: BudgetLedger | None = None):
        self.store = store
        self.budget = budget
        self.dag = TaskDAG()
        self._last_role = ""

    def submit(self, task: AgentTask) -> AgentTask:
        stored = self.store.create_task(task)
        if stored.task_id not in self.dag.tasks:
            self.dag.add(stored)
        return stored

    def refresh(self, run_id: str = "") -> None:
        self.dag = TaskDAG()
        tasks = self.store.list_tasks(run_id)
        # Persisted dependencies may be listed before their parent in priority order.
        remaining = list(tasks)
        while remaining:
            progressed = False
            for task in list(remaining):
                if all(dep in self.dag.tasks for dep in task.depends_on):
                    self.dag.add(task)
                    remaining.remove(task)
                    progressed = True
            if not progressed:
                raise ValueError(
                    "persisted collaboration tasks contain unknown dependency or cycle"
                )

    def ready(self, *, now: float | None = None) -> list[AgentTask]:
        return self.dag.ready(now=now)

    def run_once(
        self,
        worker: str,
        handler: Callable[[AgentTask], AgentResult],
        *,
        lease_seconds: float = 60.0,
    ) -> AgentResult | None:
        ready = self.ready()
        if not ready:
            return None
        # Rotate equal-priority work across roles so one role cannot monopolize
        # the scheduler while another role has runnable work.
        highest_priority = ready[0].priority
        candidates = [task for task in ready if task.priority == highest_priority]
        alternates = [task for task in candidates if task.role != self._last_role]
        task = (alternates or candidates)[0]
        reservation = ""
        if self.budget is not None:
            reservation = f"task:{task.task_id}:attempt:{task.attempt + 1}"
            decision = self.budget.reserve(
                role=task.role,
                costs=task.budget,
                reservation_id=reservation,
            )
            if not decision.allowed:
                return AgentResult(task.task_id, status=TaskStatus.BLOCKED, error=decision.reason)
        claimed = None
        try:
            claimed = self.store.claim_task(task.task_id, worker, lease_seconds=lease_seconds)
            self._last_role = claimed.role
            result = handler(claimed)
            self.store.complete_task(claimed.task_id, result, worker=worker)
            self.refresh(claimed.run_id)
            return result
        except Exception as exc:
            if claimed is None:
                raise
            result = AgentResult(
                task_id=claimed.task_id, status=TaskStatus.FAILED, error=str(exc)[:500]
            )
            self.store.complete_task(claimed.task_id, result, worker=worker)
            self.refresh(claimed.run_id)
            return result
        finally:
            if self.budget is not None and reservation:
                self.budget.release(reservation)
