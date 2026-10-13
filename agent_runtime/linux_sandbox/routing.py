"""Narrow P2 controller routing for Python commands and pytest."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from agent_runtime.path_safety import resolve_under_root
from agent_runtime.tool_result import ToolResult, ToolStatus

from .models import SandboxRequest, SandboxResult
from .permissions import permission_rejection, permission_service


def pytest_target(root: str | Path, raw: str, *, python_file_only: bool = True) -> str:
    """Validate a nodeid's file independently of its pytest selector.

    ``python_file_only`` is relaxed on the host tier, where a directory is a
    legitimate pytest target; containment and option-injection checks stay on
    in both tiers.
    """
    file_part, separator, selector = raw.partition("::")
    if not file_part or file_part.startswith("-") or (separator and not selector):
        raise ValueError("policy_denied: invalid pytest target")
    if any(part.startswith("-") for part in selector.split("::")):
        raise ValueError("policy_denied: invalid pytest selector")
    path = resolve_under_root(root, file_part)
    if not path.exists():
        raise ValueError("policy_denied: pytest target does not exist")
    if python_file_only and path.suffix != ".py":
        raise ValueError("policy_denied: pytest target must be an existing Python file")
    return path.relative_to(Path(root).resolve()).as_posix() + (
        separator + selector if separator else ""
    )


def execute_sandbox(
    context,
    operation: str,
    argv: tuple[str, ...],
    timeout_s: int,
    *,
    tool_name: str = "",
    tool_args: dict | None = None,
    base_versions: dict | None = None,
) -> SandboxResult:
    binding = getattr(context, "sandbox_task_binding", None)
    if binding is not None:
        binding.assert_context(context)
    if context.sandbox_uncertain or context.sandbox_uncertain_event.is_set():
        raise ValueError("execution_uncertain: previous cleanup unverified")
    backend = context.sandbox_backend
    if backend is None:
        raise ValueError("sandbox backend not configured")
    if hasattr(backend, "owned_versions"):
        with context.sandbox_versions_lock:
            context.sandbox_owned_versions = backend.owned_versions()
    if binding is not None and getattr(backend, "task_binding", None) != binding:
        raise ValueError("sandbox_backend_binding_mismatch")
    request = SandboxRequest(
        workspace_id=context.sandbox_workspace_id,
        task_id=context.sandbox_task_id,
        run_id=context.sandbox_run_id,
        call_id="call-" + uuid.uuid4().hex,
        operation=operation,
        argv=argv,
        timeout_s=timeout_s,
        owner_token=str(getattr(context, "sandbox_owner_token", "") or ""),
        generation=int(getattr(context, "sandbox_generation", 0) or 0),
        coordination_revision=int(getattr(context, "sandbox_coordination_revision", 0) or 0),
        schema_version="2" if operation == "workspace" else "1",
        tool_name=tool_name,
        tool_args=tool_args or {},
        base_versions=base_versions or {},
        output_limit_bytes=65536 if operation != "workspace" else 1048576,
    )
    coordinator = getattr(context, "run_coordinator", None)
    resource = None
    if coordinator is not None:
        coordinator.assert_can_dispatch()
        lease = coordinator.lease
        if binding is not None and (
            lease.run_id != binding.run_id or lease.task_id != binding.task_id
        ):
            raise ValueError("sandbox_task_identity_mismatch: coordinator")
        from dataclasses import replace

        request = replace(
            request,
            run_id=lease.run_id,
            task_id=lease.task_id,
            owner_token=lease.owner_token,
            generation=lease.generation,
            coordination_revision=lease.coordination_revision,
        )
    service = permission_service(context)
    try:
        inputs = (
            backend.permission_inputs(request) if hasattr(backend, "permission_inputs") else None
        )
        decision = service.authorize(
            request,
            callback=getattr(context, "sandbox_approval_callback", None),
            policy=getattr(context, "sandbox_approval_policy", "ask"),
            cancel_token=getattr(context, "cancel_token", None),
            restore_owned=getattr(context, "sandbox_owned_versions", {}),
            input_versions=inputs,
            coordinator=coordinator,
        )
    except (ValueError, TypeError, OSError) as exc:
        decision = {"status": str(exc).split(":", 1)[0]}
    if decision["status"] != "allowed":
        from .workspace_contract import tool_result_to_wire

        return SandboxResult(
            "cancelled" if decision["status"] == "cancelled" else "rejected",
            error_code=decision["status"],
            cleanup="confirmed",
            mutation_status="not_started",
            permission=decision,
            tool_result=tool_result_to_wire(permission_rejection(decision)),
        )
    if coordinator is not None:
        try:
            resource = coordinator.register_resource(
                resource_id=request.call_id,
                kind="sandbox_call",
                effect="write"
                if operation in {"command", "pytest"}
                or tool_name in {"write_file", "patch_file", "apply_patch"}
                else "read",
                parent_id=str(getattr(context, "sandbox_parent_resource_id", "") or ""),
                receipt_ref=request.call_id,
                payload={
                    "operation": operation,
                    "owner_token": request.owner_token,
                    "generation": request.generation,
                },
            )
            coordinator.transition_resource(resource.resource_id, "running")
        except BaseException:
            # Authorization precedes coordination so the same request is fenced
            # by both authorities. Registration failure must not leave a replayable
            # ticket, and a partially registered resource must not stay planned.
            service.discard(request.call_id)
            if resource is not None:
                try:
                    coordinator.transition_resource(
                        resource.resource_id,
                        "unknown",
                        cleanup="unverified",
                        receipt_ref=request.call_id,
                        error_code="resource_registration_failed",
                    )
                except BaseException:
                    pass
            raise
    cancel_token = getattr(context, "cancel_token", None)
    done = threading.Event()

    def cancel_when_requested():
        while not done.wait(0.05):
            if cancel_token.is_cancelled:
                while not done.wait(0.05) and not backend.cancel(request.call_id):
                    pass
                return

    watcher = None
    if cancel_token is not None:
        watcher = threading.Thread(target=cancel_when_requested, daemon=True)
        watcher.start()
    try:
        if cancel_token is not None and cancel_token.is_cancelled:
            if coordinator is not None:
                coordinator.request_cancel()
            result = SandboxResult(
                "cancelled",
                cleanup="confirmed",
                receipt_id=request.call_id,
                mutation_status="not_started",
            )
        else:
            if coordinator is not None:
                coordinator.assert_can_dispatch()
            # The receipt registry has one active call per workspace. Batch
            # contexts share this lock; waiting never falls back to host tools.
            deadline = time.monotonic() + timeout_s + 30
            acquired = False
            while time.monotonic() < deadline:
                if context.sandbox_dispatch_lock.acquire(timeout=0.05):
                    acquired = True
                    break
                if cancel_token is not None and cancel_token.is_cancelled:
                    break
            if not acquired:
                result = SandboxResult(
                    "cancelled"
                    if cancel_token is not None and cancel_token.is_cancelled
                    else "rejected",
                    cleanup="confirmed",
                    receipt_id=request.call_id,
                    mutation_status="not_started",
                    error_code=(
                        "cancelled"
                        if cancel_token is not None and cancel_token.is_cancelled
                        else "workspace_busy"
                    ),
                )
            else:
                try:
                    if context.sandbox_uncertain or context.sandbox_uncertain_event.is_set():
                        raise ValueError("execution_uncertain: previous call unverified")
                    if coordinator is not None:
                        coordinator.assert_can_dispatch()
                    if cancel_token is not None and cancel_token.is_cancelled:
                        result = SandboxResult(
                            "cancelled",
                            cleanup="confirmed",
                            receipt_id=request.call_id,
                            mutation_status="not_started",
                        )
                    else:
                        # Real backends consume under their native dispatch lock. Test
                        # transports consume here, so internal verifier calls use the same gate.
                        if not getattr(backend, "requires_authorization", False):
                            consumed = service.consume(request, input_versions=inputs)
                        result = backend.execute(request)
                        if not getattr(backend, "requires_authorization", False):
                            from dataclasses import replace

                            result = replace(result, permission=consumed)
                            service.outcome(consumed, result.execution_status, result.receipt_id)
                        elif (
                            result.execution_status not in {"rejected", "start_failed"}
                            and not result.permission
                        ):
                            # A native backend must return the exact ticket it
                            # consumed. A successful-looking reply without it
                            # cannot prove that the approved invocation ran.
                            from dataclasses import replace

                            result = replace(
                                result,
                                execution_status="uncertain",
                                error_code="authorization_unconfirmed",
                                cleanup="unverified",
                            )
                    if operation == "workspace" and result.execution_status == "completed":
                        from dataclasses import replace

                        from .workspace_contract import tool_result_from_wire, validate_versions

                        try:
                            typed = tool_result_from_wire(result.tool_result)
                            for key in ("workspace_versions", "source_dependencies"):
                                validate_versions(typed.metadata.get(key, {}))
                        except (ValueError, TypeError):
                            result = replace(
                                result,
                                execution_status="uncertain",
                                error_code="workspace_result_invalid",
                                tool_result={},
                            )
                    if (
                        result.execution_status == "uncertain"
                        or (
                            result.execution_status == "completed"
                            and (
                                result.actual_backend != "linux_sandbox"
                                or not result.receipt_id
                                or result.cleanup != "confirmed"
                            )
                        )
                        or (
                            result.cleanup != "confirmed"
                            and result.execution_status not in {"rejected", "start_failed"}
                        )
                    ):
                        context.sandbox_uncertain = True
                        context.sandbox_uncertain_event.set()
                finally:
                    context.sandbox_dispatch_lock.release()
    except BaseException as exc:
        context.sandbox_uncertain = True
        context.sandbox_uncertain_event.set()
        if coordinator is not None and resource is not None:
            try:
                coordinator.transition_resource(
                    resource.resource_id,
                    "unknown",
                    cleanup="unverified",
                    receipt_ref=request.call_id,
                    error_code="execution_uncertain",
                    payload={"execution_status": "uncertain"},
                )
            except Exception as coordination_error:
                exc.add_note(
                    "sandbox resource finalization failed: " + type(coordination_error).__name__
                )
        raise
    finally:
        service.discard(request.call_id)
        done.set()
        if watcher is not None:
            watcher.join(timeout=1)
    if coordinator is not None and resource is not None:
        status = (
            "completed"
            if result.execution_status == "completed"
            else (
                "cancelled"
                if result.execution_status == "cancelled"
                else "failed"
                if result.execution_status in {"rejected", "start_failed"}
                else "unknown"
            )
        )
        from agent_runtime.run_coordination import StaleGenerationError

        try:
            coordinator.transition_resource(
                resource.resource_id,
                status,
                cleanup="confirmed" if status == "failed" else result.cleanup,
                receipt_ref=result.receipt_id,
                error_code=result.error_code,
                payload={"execution_status": result.execution_status},
            )
        except StaleGenerationError:
            current = coordinator.store.snapshot(request.run_id)
            if current.status != "cancelled" or current.generation != request.generation:
                from dataclasses import replace

                result = replace(
                    result, execution_status="uncertain", error_code="stale_generation"
                )
    if (
        result.execution_status == "uncertain"
        or (
            result.execution_status == "completed"
            and (result.actual_backend != "linux_sandbox" or not result.receipt_id)
        )
        or (
            result.cleanup != "confirmed"
            and result.execution_status not in {"rejected", "start_failed"}
        )
    ):
        context.sandbox_uncertain = True
        context.sandbox_uncertain_event.set()
    return result


def sandbox_tool_result(result: SandboxResult, label: str) -> ToolResult:
    status = result.execution_status
    if status == "uncertain" or (
        status == "completed"
        and (
            result.cleanup != "confirmed"
            or result.actual_backend != "linux_sandbox"
            or not result.receipt_id
        )
    ):
        tool_status = ToolStatus.UNCERTAIN.value
    elif status == "cancelled":
        tool_status = ToolStatus.CANCELLED.value
    elif status == "rejected":
        tool_status = ToolStatus.REJECTED.value
    elif status == "completed" and result.exit_code == 0:
        tool_status = ToolStatus.SUCCESS.value
    else:
        tool_status = ToolStatus.ERROR.value
    output = (result.stdout_excerpt + "\n" + result.stderr_excerpt).strip()
    metadata = {
        "execution_tier": "linux_sandbox" if result.actual_backend == "linux_sandbox" else "none",
        "sandbox_status": status,
        "sandbox_cleanup": result.cleanup,
        "termination_guaranteed": result.cleanup == "confirmed",
        "sandbox_receipt_id": result.receipt_id,
        "sandbox_policy_digest": result.policy_digest,
        "sandbox_mutation_status": result.mutation_status,
        "sandbox_exit_code": result.exit_code,
        "requested_backend": result.requested_backend,
        "actual_backend": result.actual_backend,
        "sandbox_permission": result.permission,
        "approval_id": result.permission.get("approval_id", ""),
    }
    if result.tool_result:
        from .workspace_contract import tool_result_from_wire

        typed = tool_result_from_wire(result.tool_result)
        if "command_exit_code" in typed.metadata:
            typed.content = f"{label} exit={result.exit_code} status={status}\n{output}".strip()
        typed.metadata.update(metadata)
        typed.receipt = {
            "id": result.receipt_id,
            "policy_digest": result.policy_digest,
            "permission": result.permission,
        }
        typed.duration_ms = result.duration_ms
        if tool_status != ToolStatus.SUCCESS.value:
            typed.status = ToolStatus(tool_status)
            typed.error_code = (
                result.error_code
                or typed.error_code
                or ("cancelled" if tool_status == ToolStatus.CANCELLED.value else "")
            )
            typed.retryable = False
        return typed
    return ToolResult(
        content=f"{label} exit={result.exit_code} status={status}\n{output}".strip(),
        status=tool_status,
        error_code=result.error_code
        or (
            "cancelled"
            if tool_status == ToolStatus.CANCELLED.value
            else "command_failed"
            if tool_status == "error"
            else ""
        ),
        retryable=False,
        metadata=metadata,
        receipt={
            "id": result.receipt_id,
            "policy_digest": result.policy_digest,
            "permission": result.permission,
        },
        output_truncated=result.output_truncated,
        duration_ms=result.duration_ms,
    )
