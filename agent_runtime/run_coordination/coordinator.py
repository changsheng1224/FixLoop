"""Run-level owner fencing and resource cancellation orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import CancelReport, OwnerLease, ResourceRecord, ResourceResult
from .store import (
    CoordinationError,
    CoordinationIntegrityError,
    RunCoordinationStore,
)


class ResourceAdapter(Protocol):
    """Adapter for a resource with a durable cancel/reconcile protocol."""

    def cancel(self, resource: ResourceRecord, request_id: str) -> ResourceResult: ...

    def reconcile(self, resource: ResourceRecord) -> ResourceResult: ...


@dataclass(frozen=True)
class UnknownResourceAdapter:
    """Fail closed when a resource has no controlled cleanup implementation."""

    def cancel(self, resource: ResourceRecord, request_id: str) -> ResourceResult:
        return ResourceResult(
            resource.resource_id,
            "unknown",
            cleanup="unknown",
            error_code="resource_adapter_missing",
        )

    def reconcile(self, resource: ResourceRecord) -> ResourceResult:
        return ResourceResult(
            resource.resource_id,
            "unknown",
            cleanup="unknown",
            error_code="resource_adapter_missing",
        )


class RunCoordinator:
    """Coordinates one resumable run without becoming a second event journal."""

    def __init__(
        self,
        workspace: str,
        task_id: str,
        run_id: str,
        *,
        state_root: str = "",
        lease_seconds: float = 30.0,
        adapters: dict[str, ResourceAdapter] | None = None,
    ) -> None:
        self.workspace = workspace
        self.task_id = task_id
        self.run_id = run_id
        self.lease_seconds = lease_seconds
        self.store = RunCoordinationStore(workspace, state_root)
        self.adapters = dict(adapters or {})
        self.lease: OwnerLease | None = None
        self.cancel_token = None

    def _require_lease(self) -> OwnerLease:
        if self.lease is None:
            raise CoordinationError("resume_owner_missing")
        return self.lease

    def _adapter(self, resource: ResourceRecord) -> ResourceAdapter:
        return (
            self.adapters.get(resource.kind) or self.adapters.get("*") or UnknownResourceAdapter()
        )

    def acquire(self) -> OwnerLease:
        self.lease = self.store.acquire(
            self.task_id,
            self.run_id,
            lease_seconds=self.lease_seconds,
        )
        return self.lease

    def heartbeat(self) -> OwnerLease:
        self.lease = self.store.heartbeat(self._require_lease(), lease_seconds=self.lease_seconds)
        return self.lease

    def assert_lease(self, *, allow_cancelling: bool = False) -> None:
        self.store.assert_lease(self._require_lease(), allow_cancelling=allow_cancelling)

    def assert_can_dispatch(self) -> None:
        if self.cancel_token is not None and self.cancel_token.is_cancelled:
            self.request_cancel()
        self.store.assert_lease(self._require_lease())

    def reconcile(self) -> dict[str, Any]:
        lease = self._require_lease()
        snapshot = self.store.snapshot(self.run_id)
        results: list[ResourceResult] = []
        # Children are reconciled before parents so a parent never masks an
        # unconfirmed sandbox or subagent underneath it.
        for resource in self._reverse_depth(snapshot.resources):
            if (
                resource.status in {"completed", "failed", "cancelled", "not_started"}
                and resource.cleanup == "confirmed"
            ):
                result = ResourceResult(
                    resource.resource_id,
                    resource.status,
                    cleanup="confirmed",
                    receipt_ref=resource.receipt_ref,
                )
            else:
                result = self._safe_result(resource, "reconcile")
            results.append(result)
            self.store.transition_resource(
                lease,
                resource.resource_id,
                result.status
                if result.status in {"completed", "failed", "cancelled", "not_started", "unknown"}
                else "unknown",
                cleanup=result.cleanup,
                receipt_ref=result.receipt_ref,
                error_code=result.error_code,
                payload=result.details,
                adopt_generation=True,
            )
        snapshot = self.store.snapshot(self.run_id)
        unknown = [r for r in results if not r.confirmed]
        if unknown:
            self.store.mark_recovery_required(lease, error_code="old_execution_unconfirmed")
            return {"status": "recovery_required", "resources": [r.to_dict() for r in results]}
        if snapshot.cancel_request_id:
            self.store.request_cancel(lease, snapshot.cancel_request_id)
            self.lease = self.store.begin_cancelling(lease)
            # A replacement owner must finish an already persisted cancel request;
            # leaving the run in cancelling would leak resources indefinitely.
            report = self.cancel(snapshot.cancel_request_id)
            return {"status": report.status, "resources": [r.to_dict() for r in report.resources]}
        self.lease = self.store.activate(lease)
        return {"status": "active", "resources": [r.to_dict() for r in results]}

    def _safe_result(self, resource, method, *args):
        try:
            result = getattr(self._adapter(resource), method)(resource, *args)
            if result.resource_id not in {"", resource.resource_id}:
                raise CoordinationIntegrityError("resource_result_identity_mismatch")
            return result
        except Exception as exc:
            return ResourceResult(
                resource.resource_id,
                "unknown",
                cleanup="unknown",
                error_code="resource_cleanup_exception",
                details={"exception": type(exc).__name__},
            )

    @staticmethod
    def _reverse_depth(resources: tuple[ResourceRecord, ...]) -> list[ResourceRecord]:
        by_id = {r.resource_id: r for r in resources}

        def depth(resource: ResourceRecord) -> int:
            value = 0
            seen: set[str] = set()
            parent = resource.parent_id
            while parent and parent in by_id and parent not in seen:
                seen.add(parent)
                value += 1
                parent = by_id[parent].parent_id
            return value

        return sorted(resources, key=lambda r: (-depth(r), r.created_at, r.resource_id))

    def register_resource(self, **kwargs: Any) -> ResourceRecord:
        self.assert_can_dispatch()
        return self.store.register_resource(self._require_lease(), **kwargs)

    def register_recovery_resource(self, **kwargs: Any) -> ResourceRecord:
        """Register a pre-existing external task while the owner is reconciling."""
        lease = self._require_lease()
        return self.store.register_resource(lease, allow_reconciling=True, **kwargs)

    def transition_resource(self, resource_id: str, status: str, **kwargs: Any) -> ResourceRecord:
        return self.store.transition_resource(self._require_lease(), resource_id, status, **kwargs)

    def dispatch(self, resource: ResourceRecord, runner) -> Any:
        """Run a registered resource only while the same generation is active."""
        self.assert_can_dispatch()
        self.store.transition_resource(self._require_lease(), resource.resource_id, "running")
        try:
            result = runner()
        except BaseException as exc:
            self.store.transition_resource(
                self._require_lease(),
                resource.resource_id,
                "unknown",
                cleanup="unknown",
                error_code="resource_execution_uncertain",
                payload={"exception": type(exc).__name__},
            )
            raise
        status = "completed" if getattr(result, "ok", True) else "failed"
        cleanup = "confirmed" if getattr(result, "execution_stopped", True) else "unknown"
        self.store.transition_resource(
            self._require_lease(), resource.resource_id, status, cleanup=cleanup
        )
        return result

    def request_cancel(self, request_id: str = "") -> str:
        lease = self._require_lease()
        current = self.store.snapshot(self.run_id)
        if (
            current.generation == lease.generation
            and not current.owner_token
            and current.status in {"cancelled", "recovery_required"}
            and current.cancel_request_id
        ):
            return current.cancel_request_id
        return self.store.request_cancel(lease, request_id)

    def cancel(self, request_id: str = "", *, finalize: bool = True) -> CancelReport:
        """Confirm cleanup; callers may retain the closed fence for safe rollback."""
        current = self.store.snapshot(self.run_id)
        if current.status == "cancelled":
            return CancelReport(
                self.run_id,
                current.cancel_request_id or request_id,
                "cancelled",
                tuple(
                    ResourceResult(
                        r.resource_id, r.status, cleanup="confirmed", receipt_ref=r.receipt_ref
                    )
                    for r in current.resources
                ),
            )
        if (
            current.status == "recovery_required"
            and current.cancel_request_id
            and not current.owner_token
            and current.generation == self._require_lease().generation
        ):
            # The failed cleanup released the fence. Repeated cancellation only
            # returns persisted facts; another cleanup attempt needs a new owner.
            return CancelReport(
                self.run_id,
                current.cancel_request_id,
                current.status,
                tuple(
                    ResourceResult(
                        r.resource_id,
                        r.status,
                        cleanup=r.cleanup,
                        receipt_ref=r.receipt_ref,
                        error_code=r.error_code,
                    )
                    for r in current.resources
                ),
                "resource_cleanup_failed",
            )
        lease = self._require_lease()
        request_id = self.store.request_cancel(lease, request_id)
        self.lease = self.store.begin_cancelling(lease)
        results: list[ResourceResult] = []
        for resource in self._reverse_depth(self.store.resources(self.run_id)):
            if (
                resource.status in {"completed", "failed", "cancelled", "not_started"}
                and resource.cleanup == "confirmed"
            ):
                results.append(
                    ResourceResult(
                        resource.resource_id,
                        resource.status,
                        cleanup="confirmed",
                        receipt_ref=resource.receipt_ref,
                    )
                )
                continue
            self.store.record_event(
                self._require_lease(),
                "resource_cancel_sent",
                {
                    "resource_id": resource.resource_id,
                    "kind": resource.kind,
                    "request_id": request_id,
                },
            )
            result = self._safe_result(resource, "cancel", request_id)
            results.append(result)
            target_status = (
                result.status
                if result.status in {"cancelled", "completed", "failed", "not_started", "unknown"}
                else "unknown"
            )
            self.store.transition_resource(
                self._require_lease(),
                resource.resource_id,
                target_status,
                cleanup=result.cleanup,
                receipt_ref=result.receipt_ref,
                error_code=result.error_code,
                payload=result.details,
            )
            self.store.record_event(
                self._require_lease(),
                "resource_cleanup_confirmed" if result.confirmed else "resource_cleanup_failed",
                {"resource_id": resource.resource_id, "error_code": result.error_code},
            )
        report_status = "cancelled" if all(r.confirmed for r in results) else "recovery_required"
        if report_status == "cancelled" and finalize:
            self.store.finish(self._require_lease(), "cancelled")
        elif report_status != "cancelled":
            self.store.mark_recovery_required(
                self._require_lease(), error_code="resource_cleanup_failed"
            )
        return CancelReport(
            self.run_id,
            request_id,
            report_status,
            tuple(results),
            "" if report_status == "cancelled" else "resource_cleanup_failed",
        )

    def finish(self, status: str, *, error_code: str = ""):
        return self.store.finish(self._require_lease(), status, error_code=error_code)
