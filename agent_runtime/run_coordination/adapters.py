"""Resource adapters backed by the existing Plan, collaboration, and sandbox journals."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent_runtime.plan_runtime.processes import confirmed_exited

from .models import ResourceRecord, ResourceResult


def _terminal_result(
    resource: ResourceRecord, status: str, *, receipt_ref: str = ""
) -> ResourceResult:
    return ResourceResult(
        resource.resource_id,
        status,
        cleanup="confirmed",
        receipt_ref=receipt_ref or resource.receipt_ref,
    )


class PlanAttemptAdapter:
    """Project PlanStore attempt facts into the coordination resource state."""

    def __init__(self, session):
        self.session = session

    def _attempt(self, resource: ResourceRecord) -> dict[str, Any] | None:
        attempts = self.session.store.latest("attempt", "attempt_id")
        return attempts.get(resource.resource_id)

    def _operations(self, attempt_id: str) -> list[dict[str, Any]]:
        return self.session.operations(attempt_id)

    def reconcile(self, resource: ResourceRecord) -> ResourceResult:
        attempt = self._attempt(resource)
        if attempt is None:
            return _terminal_result(resource, "not_started")
        phase = attempt.get("phase")
        if phase == "reconciled":
            terminal = attempt.get("terminal_status", "")
            mapped = {
                "succeeded": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "not_started": "not_started",
            }.get(terminal)
            if mapped:
                return _terminal_result(resource, mapped)
            return ResourceResult(
                resource.resource_id,
                "unknown",
                cleanup="unknown",
                error_code="plan_terminal_status_missing",
            )
        operations = self._operations(resource.resource_id)
        if phase == "prepared" and not operations:
            return _terminal_result(resource, "not_started")
        if (
            phase in {"prepared", "dispatched"}
            and operations
            and confirmed_exited(attempt.get("owner", {}))
        ):
            # A process can die after the tool operation is durably recorded
            # but before PlanSession records the attempt result.  The
            # operation receipt is the authoritative fact for this window;
            # recover() will append the missing attempt result and settle the
            # DAG node idempotently.
            complete_ops = all(
                item.get("phase") == "result_recorded" and item.get("execution_stopped")
                for item in operations
            )
            if complete_ops:
                statuses = {str(item.get("status") or "") for item in operations}
                if statuses == {"success"}:
                    return _terminal_result(resource, "completed")
                if statuses == {"cancelled"}:
                    return _terminal_result(resource, "cancelled")
                if statuses and statuses <= {"failed", "rejected"}:
                    return _terminal_result(resource, "failed")
        if phase == "result_recorded" and operations:
            stopped = all(
                item.get("phase") == "result_recorded" and item.get("execution_stopped")
                for item in operations
            )
            result = attempt.get("result", {})
            if stopped and result.get("status") != "uncertain":
                mapped = {"success": "completed", "cancelled": "cancelled", "failed": "failed"}.get(
                    result.get("status")
                )
                if mapped:
                    return _terminal_result(resource, mapped)
        return ResourceResult(
            resource.resource_id,
            "unknown",
            cleanup="unknown",
            error_code="plan_attempt_outcome_unconfirmed",
        )

    def cancel(self, resource: ResourceRecord, request_id: str) -> ResourceResult:
        return self.reconcile(resource)


class CollaborationTaskAdapter:
    """Project AgentTask state; a running task is never guessed to be stopped."""

    def __init__(self, store, run_id: str):
        self.store = store
        self.run_id = run_id

    def _task(self, resource: ResourceRecord):
        return self.store.get_task(resource.payload.get("task_id", resource.resource_id))

    @staticmethod
    def _status(task) -> str:
        value = str(task.status)
        return {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "pending": "not_started",
            "ready": "not_started",
        }.get(value, "unknown")

    def reconcile(self, resource: ResourceRecord) -> ResourceResult:
        task = self._task(resource)
        if task is None:
            return _terminal_result(resource, "not_started")
        mapped = self._status(task)
        if mapped != "unknown":
            return _terminal_result(resource, mapped)
        owner = resource.payload.get("owner", {})
        if resource.payload.get("execution_mode") == "local_phase" and confirmed_exited(owner):
            return ResourceResult(
                resource.resource_id,
                "failed",
                cleanup="confirmed",
                error_code="agent_task_owner_exited",
                details={"owner": owner},
            )
        return ResourceResult(
            resource.resource_id,
            "unknown",
            cleanup="unknown",
            error_code="agent_task_still_running",
        )

    def cancel(self, resource: ResourceRecord, request_id: str) -> ResourceResult:
        self.store.request_cancel_task(
            resource.payload.get("task_id", resource.resource_id), request_id
        )
        task = self._task(resource)
        if task is None:
            return _terminal_result(resource, "not_started")
        mapped = self._status(task)
        if mapped != "unknown":
            return _terminal_result(resource, mapped)
        if resource.payload.get("execution_mode") == "local_phase" and confirmed_exited(
            resource.payload.get("owner", {})
        ):
            return self.reconcile(resource)
        # A running subagent may have descendants outside this process. Keep the
        # run recoverable until its worker publishes a terminal result.
        return ResourceResult(
            resource.resource_id,
            "unknown",
            cleanup="unknown",
            error_code="agent_task_process_unconfirmed",
        )


class SandboxCallAdapter:
    """Use the sandbox receipt as the source of truth for process cleanup."""

    def __init__(self, backend):
        self.backend = backend

    @staticmethod
    def _result(resource: ResourceRecord, receipt: dict[str, Any]) -> ResourceResult:
        result = receipt.get("result", {})
        status = result.get("execution_status", "uncertain")
        mapped = {
            "completed": "completed",
            "cancelled": "cancelled",
            "start_failed": "failed",
            "rejected": "failed",
            "timeout": "failed",
        }.get(status, "unknown")
        cleanup = result.get("cleanup", "unknown")
        if status == "start_failed" and receipt.get("no_target_started") is True:
            cleanup = "confirmed"
        if mapped == "unknown" or cleanup != "confirmed":
            return ResourceResult(
                resource.resource_id,
                "unknown",
                cleanup="unknown",
                receipt_ref=receipt.get("call_id", resource.receipt_ref),
                error_code="sandbox_cleanup_unconfirmed",
                details={"execution_status": status},
            )
        return ResourceResult(
            resource.resource_id,
            mapped,
            cleanup="confirmed",
            receipt_ref=receipt.get("call_id", resource.receipt_ref),
            details={"execution_status": status},
        )

    def _current(self, resource):
        if hasattr(self.backend, "inspect_receipt"):
            return self.backend.inspect_receipt(resource.resource_id)
        return self.backend.store.inspect(resource.resource_id)

    def reconcile(self, resource: ResourceRecord) -> ResourceResult:
        current = self._current(resource)
        return self._reconcile_receipt(resource, current)

    def _reconcile_receipt(self, resource, current):
        if not current:
            if resource.status == "planned":
                return _terminal_result(resource, "not_started")
            return ResourceResult(
                resource.resource_id,
                "unknown",
                cleanup="unknown",
                error_code="sandbox_receipt_missing",
            )
        if current.get("call_id") != resource.resource_id:
            return ResourceResult(
                resource.resource_id,
                "unknown",
                cleanup="unknown",
                error_code="sandbox_receipt_identity_mismatch",
            )
        if any(
            current.get(k) != resource.payload.get(k)
            for k in ("owner_token", "generation")
            if k in resource.payload
        ):
            return ResourceResult(
                resource.resource_id,
                "unknown",
                cleanup="unknown",
                error_code="sandbox_receipt_owner_mismatch",
            )
        if current.get("state") == "terminal":
            return self._result(resource, current)
        return ResourceResult(
            resource.resource_id,
            "unknown",
            cleanup="unknown",
            error_code="sandbox_process_still_running",
        )

    def cancel(self, resource: ResourceRecord, request_id: str) -> ResourceResult:
        current = self._current(resource)
        checked = self._reconcile_receipt(resource, current)
        if checked.confirmed or checked.error_code != "sandbox_process_still_running":
            return checked
        if current.get("state") == "planned":
            return _terminal_result(resource, "not_started", receipt_ref=resource.receipt_ref)
        try:
            self.backend.cancel(resource.resource_id)
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            current = self._current(resource)
            checked = self._reconcile_receipt(resource, current)
            if checked.confirmed or checked.error_code != "sandbox_process_still_running":
                return checked
            time.sleep(0.05)
        return ResourceResult(
            resource.resource_id, "unknown", cleanup="unknown", error_code="sandbox_cancel_timeout"
        )


@dataclass
class CoordinationAdapters:
    plan: PlanAttemptAdapter
    agents: CollaborationTaskAdapter | None = None
    sandbox: SandboxCallAdapter | None = None

    def mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"plan_attempt": self.plan}
        if self.agents is not None:
            result["agent_task"] = self.agents
        if self.sandbox is not None:
            result["sandbox_call"] = self.sandbox
        return result
