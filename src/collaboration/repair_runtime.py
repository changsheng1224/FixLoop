"""Adapter that maps the existing repair phases onto durable collaboration tasks."""

from __future__ import annotations

from src.collaboration.contracts import AgentResult, AgentTask, TaskStatus
from src.collaboration.effects import EffectLedger
from src.collaboration.scheduler import TaskScheduler
from src.collaboration.store import CollaborationStore


class RepairCollaborationRuntime:
    """Keep phase execution and durable task state aligned.

    The existing Orchestrator remains the policy owner. This adapter records
    task claims/completions and receipts without allowing a task worker to
    bypass the repair FSM.
    """

    def __init__(self, repo_root: str, run_id: str, state):
        self.store = CollaborationStore(repo_root)
        self.scheduler = TaskScheduler(self.store)
        self.effects = EffectLedger(self.store)
        self.run_id = run_id
        self.worker = "orchestrator"
        self._ensure_plan(state)

    def _ensure_plan(self, state) -> None:
        tasks = self.store.list_tasks(self.run_id)
        if not tasks:
            tasks = [
                AgentTask(
                    task_id=f"{self.run_id}:context",
                    run_id=self.run_id,
                    role="context",
                    kind="context_projection",
                    phase="context",
                ),
                AgentTask(
                    task_id=f"{self.run_id}:patch",
                    run_id=self.run_id,
                    role="patcher",
                    kind="candidate_patch",
                    phase="patch",
                    depends_on=[f"{self.run_id}:context"],
                ),
                AgentTask(
                    task_id=f"{self.run_id}:verify",
                    run_id=self.run_id,
                    role="verifier",
                    kind="verification",
                    phase="verify",
                    depends_on=[f"{self.run_id}:patch"],
                ),
            ]
            for task in tasks:
                self.store.create_task(task)
        self.scheduler.refresh(self.run_id)
        self.sync_state(state)

    def _task(self, phase: str) -> AgentTask | None:
        mapping = {"context": "context", "patch": "patch", "verify": "verify"}
        suffix = mapping.get(phase)
        if suffix is None:
            return None
        return self.store.get_task(f"{self.run_id}:{suffix}")

    def _complete(self, task: AgentTask, status: TaskStatus) -> None:
        if task.status == TaskStatus.RUNNING:
            self.store.complete_task(
                task.task_id,
                AgentResult(task.task_id, status=status),
                worker=self.worker,
            )
        elif task.status in {TaskStatus.PENDING, TaskStatus.READY}:
            claimed = self.store.claim_task(task.task_id, self.worker)
            self.store.complete_task(
                claimed.task_id,
                AgentResult(claimed.task_id, status=status),
                worker=self.worker,
            )

    def advance(self, phase: str, state, *, terminal_status: str = "") -> None:
        """Claim the current phase and complete its predecessor."""
        normalized = str(phase)
        if normalized == "patch":
            context = self._task("context")
            if context:
                self._complete(context, TaskStatus.COMPLETED)
            current = self._task("patch")
        elif normalized == "verify":
            patch = self._task("patch")
            if patch:
                self._complete(patch, TaskStatus.COMPLETED)
            current = self._task("verify")
        elif normalized == "context":
            current = self._task("context")
        elif normalized in {"done", "failed"}:
            current = self._task("verify")
            if current:
                self._complete(
                    current,
                    TaskStatus.COMPLETED if terminal_status == "fixed" else TaskStatus.FAILED,
                )
            self.sync_state(state)
            return
        else:
            current = None
        if current and current.status in {TaskStatus.PENDING, TaskStatus.READY}:
            self.store.claim_task(current.task_id, self.worker)
        self.scheduler.refresh(self.run_id)
        self.sync_state(state)

    def sync_state(self, state) -> None:
        tasks = self.store.list_tasks(self.run_id)
        state.collaboration_tasks = [task.to_dict() for task in tasks]
        state.task_dag_snapshot = self.scheduler.dag.snapshot()
        state.effect_receipts = self.effects.snapshot()
