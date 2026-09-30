"""Narrow P2 controller routing for Python commands and pytest."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from agent_runtime.path_safety import resolve_under_root
from agent_runtime.tool_result import ToolResult, ToolStatus

from .models import SandboxRequest, SandboxResult


def pytest_target(root: str | Path, raw: str) -> str:
    """Validate a nodeid's file independently of its pytest selector."""
    file_part, separator, selector = raw.partition("::")
    if not file_part or file_part.startswith("-") or (separator and not selector):
        raise ValueError("policy_denied: invalid pytest target")
    if any(part.startswith("-") for part in selector.split("::")):
        raise ValueError("policy_denied: invalid pytest selector")
    path = resolve_under_root(root, file_part)
    if not path.exists() or path.suffix != ".py":
        raise ValueError("policy_denied: pytest target must be an existing Python file")
    return path.relative_to(Path(root).resolve()).as_posix() + (
        separator + selector if separator else ""
    )


def execute_sandbox(
    context, operation: str, argv: tuple[str, ...], timeout_s: int
) -> SandboxResult:
    if context.sandbox_uncertain:
        raise ValueError("execution_uncertain: previous cleanup unverified")
    backend = context.sandbox_backend
    if backend is None:
        raise ValueError("sandbox backend not configured")
    request = SandboxRequest(
        workspace_id=context.sandbox_workspace_id,
        task_id=context.sandbox_task_id,
        run_id=context.sandbox_run_id,
        call_id="call-" + uuid.uuid4().hex,
        operation=operation,
        argv=argv,
        timeout_s=timeout_s,
    )
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
        result = backend.execute(request)
    except BaseException:
        context.sandbox_uncertain = True
        raise
    finally:
        done.set()
        if watcher is not None:
            watcher.join(timeout=1)
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
        "sandbox_receipt_id": result.receipt_id,
        "sandbox_policy_digest": result.policy_digest,
        "sandbox_mutation_status": result.mutation_status,
        "sandbox_exit_code": result.exit_code,
        "requested_backend": result.requested_backend,
        "actual_backend": result.actual_backend,
    }
    return ToolResult(
        content=f"{label} exit={result.exit_code} status={status}\n{output}".strip(),
        status=tool_status,
        error_code=result.error_code or ("command_failed" if tool_status == "error" else ""),
        retryable=False,
        metadata=metadata,
        receipt={"id": result.receipt_id, "policy_digest": result.policy_digest},
        output_truncated=result.output_truncated,
        duration_ms=result.duration_ms,
    )
