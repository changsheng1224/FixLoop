"""Adapter that maps the existing repair phases onto durable collaboration tasks."""

from __future__ import annotations

from agent_runtime.run_coordination.models import process_owner_identity
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

    def _owns_phase(self, task: AgentTask) -> bool:
        return task.task_id in {
            f"{self.run_id}:{phase}" for phase in ("context", "patch", "verify")
        }

    def attach_coordinator(self, coordinator) -> None:
        """Expose durable AgentTask records in the run resource DAG."""
        self.coordinator = coordinator
        existing = {item.resource_id for item in coordinator.store.resources(self.run_id)}
        tasks = self.store.list_tasks(self.run_id)
        for task in tasks:
            if "exploration" in task.payload:
                continue  # ExplorationRuntime owns its read resource and recovery adapter.
            resource_id = f"agent:{task.task_id}"
            if resource_id in existing:
                continue
            coordinator.register_recovery_resource(
                resource_id=resource_id,
                kind="agent_task",
                effect="write",
                payload={
                    "task_id": task.task_id,
                    "role": task.role,
                    "phase": task.phase,
                    "depends_on": list(task.depends_on),
                    # These tasks are phase workers owned by this runtime
                    # process.  A replacement can distinguish a crashed
                    # owner from a still-running external subagent.
                    "owner": process_owner_identity()
                    if self._owns_phase(task)
                    else task.payload.get("owner", {}),
                    "execution_mode": "local_phase" if self._owns_phase(task) else "subagent",
                },
            )
            existing.add(resource_id)
        self._sync_resources()

    def _sync_resources(self) -> None:
        coordinator = getattr(self, "coordinator", None)
        if coordinator is None:
            return
        if coordinator.store.snapshot(self.run_id).status in {
            "released",
            "failed",
            "cancelled",
            "recovery_required",
        }:
            return
        by_id = {r.resource_id: r for r in coordinator.store.resources(self.run_id)}
        for task in self.store.list_tasks(self.run_id):
            resource_id = f"agent:{task.task_id}"
            resource = by_id.get(resource_id)
            if resource is None:
                continue
            status = str(task.status)
            if status == "running":
                cleanup = "unverified"
            elif status in {"completed", "failed", "cancelled"}:
                cleanup = "confirmed"
            elif status == "expired":
                status, cleanup = "unknown", "unknown"
            else:
                status, cleanup = "planned", "unverified"
            if resource.status == status and resource.cleanup == cleanup:
                continue
            coordinator.transition_resource(
                resource_id,
                status,
                cleanup=cleanup,
                payload=resource.payload,
            )

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
        coordinator = getattr(self, "coordinator", None)
        if coordinator is not None:
            if coordinator.cancel_token is not None and coordinator.cancel_token.is_cancelled:
                self.finish_cancelled(state)
                return
            coordinator.assert_can_dispatch()
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

    def finish_cancelled(self, state) -> None:
        """Called after this runtime's phase callback has returned."""
        for task in self.store.list_tasks(self.run_id):
            if not self._owns_phase(task):
                self.store.request_cancel_task(task.task_id, "phase_cancelled")
                continue
            if task.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}:
                self._complete(task, TaskStatus.CANCELLED)
        self.sync_state(state)

    def sync_state(self, state) -> None:
        self._sync_resources()
        tasks = self.store.list_tasks(self.run_id)
        state.collaboration_tasks = [task.to_dict() for task in tasks]
        state.task_dag_snapshot = self.scheduler.dag.snapshot()
        state.effect_receipts = self.effects.snapshot()
