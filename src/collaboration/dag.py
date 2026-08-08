"""Deterministic task DAG with dependency and terminal-state governance."""

from __future__ import annotations

from dataclasses import dataclass

from src.collaboration.contracts import AgentTask, TaskStatus


class TaskDAGError(ValueError):
    pass


@dataclass(frozen=True)
class DAGDecision:
    task_id: str
    action: str
    reason: str


class TaskDAG:
    def __init__(self):
        self.tasks: dict[str, AgentTask] = {}

    def add(self, task: AgentTask) -> None:
        errors = task.validate()
        if errors:
            raise TaskDAGError("; ".join(errors))
        if task.task_id in self.tasks:
            raise TaskDAGError(f"duplicate task_id: {task.task_id}")
        unknown = [dep for dep in task.depends_on if dep not in self.tasks]
        if unknown:
            raise TaskDAGError(f"unknown dependencies: {unknown}")
        self.tasks[task.task_id] = task
        if self._has_cycle():
            self.tasks.pop(task.task_id, None)
            raise TaskDAGError("task dependency cycle")

    def _has_cycle(self) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> bool:
            if task_id in visiting:
                return True
            if task_id in visited:
                return False
            visiting.add(task_id)
            for dep in self.tasks[task_id].depends_on:
                if dep in self.tasks and visit(dep):
                    return True
            visiting.remove(task_id)
            visited.add(task_id)
            return False

        return any(visit(task_id) for task_id in self.tasks)

    def completed_ids(self) -> set[str]:
        return {
            task_id for task_id, task in self.tasks.items() if task.status == TaskStatus.COMPLETED
        }

    def ready(self, *, now: float | None = None) -> list[AgentTask]:
        completed = self.completed_ids()
        rows = [task for task in self.tasks.values() if task.ready(completed, now=now)]
        rows.sort(key=lambda task: (-task.priority, task.created_at, task.task_id))
        return rows

    def transition(self, task_id: str, status: TaskStatus | str) -> DAGDecision:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskDAGError(f"unknown task_id: {task_id}")
        target = TaskStatus(str(status))
        allowed = {
            TaskStatus.PENDING: {
                TaskStatus.READY,
                TaskStatus.RUNNING,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
                TaskStatus.EXPIRED,
            },
            TaskStatus.READY: {
                TaskStatus.RUNNING,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
                TaskStatus.EXPIRED,
            },
            TaskStatus.RUNNING: {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.EXPIRED,
                TaskStatus.READY,
            },
            TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED},
        }
        if task.status == target:
            return DAGDecision(task_id, target.value, "idempotent")
        if target not in allowed.get(task.status, set()):
            raise TaskDAGError(f"invalid transition {task.status.value}->{target.value}")
        task.status = target
        return DAGDecision(task_id, target.value, "accepted")

    def cancel_dependents(self, task_id: str, reason: str = "dependency_failed") -> list[str]:
        cancelled: list[str] = []
        for task in self.tasks.values():
            if task.status.terminal or task_id not in task.depends_on:
                continue
            task.status = TaskStatus.CANCELLED
            cancelled.append(task.task_id)
            cancelled.extend(self.cancel_dependents(task.task_id, reason))
        return cancelled

    def snapshot(self) -> dict:
        return {"tasks": {task_id: task.to_dict() for task_id, task in self.tasks.items()}}

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> TaskDAG:
        dag = cls()
        pending = [AgentTask.from_dict(item) for item in (snapshot.get("tasks") or {}).values()]
        for task in pending:
            errors = task.validate()
            if errors:
                raise TaskDAGError("; ".join(errors))
            dag.tasks[task.task_id] = task
        unknown = [
            dep
            for task in dag.tasks.values()
            for dep in task.depends_on
            if dep not in dag.tasks
        ]
        if unknown:
            raise TaskDAGError(f"unknown dependencies in snapshot: {sorted(set(unknown))}")
        if dag._has_cycle():
            raise TaskDAGError("task dependency cycle in snapshot")
        return dag
