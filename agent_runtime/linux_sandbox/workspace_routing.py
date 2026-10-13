"""Workspace tool routing and external permissions; never read host source files."""

from __future__ import annotations

from agent_runtime.tool_result import ToolResult

from .routing import execute_sandbox, sandbox_tool_result
from .workspace_contract import WORKSPACE_TOOLS, WORKSPACE_WRITE_TOOLS, relative_path


def sandbox_enabled(context):
    return context.sandbox_task_binding is not None or context.sandbox_backend is not None


def execute_workspace_tool(
    context, name, args, *, mark_read=True, record_versions=True, timeout_s=30
):
    if name not in WORKSPACE_TOOLS:
        return ToolResult.error(
            "Error: workspace tool not migrated", code="policy_denied", retryable=False
        )
    try:
        versions = {}
        if name in WORKSPACE_WRITE_TOOLS:
            from agent_runtime.tools import _reject_if_edit_locked, _reject_if_write_serial

            serial_error = _reject_if_write_serial(context)
            if serial_error:
                return ToolResult.error(serial_error, code="write_serial", retryable=False)
            if name == "apply_patch":
                from agent_runtime.apply_patch_format import parse_apply_patch_text

                patch = args.get("patch") or args.get("diff") or args.get("input") or ""
                operations = parse_apply_patch_text(patch)
                paths = [relative_path(op.path) for op in operations]
                additions = {relative_path(op.path) for op in operations if op.action == "add"}
            else:
                paths = [relative_path(args.get("path", ""))]
                additions = set(paths) if name == "write_file" else set()
            with context.sandbox_versions_lock:
                for path in paths:
                    locked = _reject_if_edit_locked(context, path)
                    if locked:
                        return ToolResult.error(locked, code="edit_lock_rejected", retryable=False)
                    if path in context.sandbox_workspace_versions:
                        versions[path] = context.sandbox_workspace_versions[path]
                    elif path in additions:
                        # Conditional create; the broker must prove absence.
                        versions[path] = None
                    else:
                        return ToolResult.error(
                            "Error: read the target before editing",
                            code="stale_precondition",
                            retryable=True,
                        )
            for key in ("base_hash", "base_sha256"):
                if key in args and (len(paths) != 1 or args[key] != versions[paths[0]]):
                    return ToolResult.error(
                        "Error: explicit base differs from read evidence",
                        code="stale_precondition",
                        retryable=True,
                    )
        execution = execute_sandbox(
            context,
            "workspace",
            (),
            timeout_s,
            tool_name=name,
            tool_args=args,
            base_versions=versions,
        )
        result = sandbox_tool_result(execution, name)
        if result.ok and record_versions:
            with context.sandbox_versions_lock:
                context.sandbox_workspace_versions.update(
                    result.metadata.get("workspace_versions", {})
                )
        if name in WORKSPACE_WRITE_TOOLS:
            with context.sandbox_versions_lock:
                for path in result.changed_files:
                    context.sandbox_workspace_versions.pop(path, None)
                context.sandbox_owned_versions.update(result.metadata.get("committed_versions", {}))
            lock = context.edit_lock
            if result.changed_files:
                from agent_runtime.tools import _mark_write_done

                _mark_write_done(context)
                if lock is not None and name == "apply_patch" and result.ok:
                    lock.apply_patch_ok_count += 1
            if lock is not None and result.error_code == "edit_lint_reject":
                lock.edit_lint_reject_count += 1
        elif mark_read and name in {"read_file", "inspect_file"} and result.ok:
            from agent_runtime.tools import _mark_edit_lock_read

            _mark_edit_lock_read(context, str(args.get("path", "")), result=result)
        return result
    except (ValueError, TypeError) as exc:
        return ToolResult.error(
            "Error: " + str(exc), code=str(exc).split(":", 1)[0], retryable=False
        )


def bind_workspace_tools(context, registry):
    if not sandbox_enabled(context):
        return registry
    from .tool_policy import SandboxToolAccess, sandbox_tool_access

    for name, spec in registry.items():
        if name.startswith("workspace_"):
            # Internal snapshot/restore operations are runtime APIs, not model tools.
            spec["lifecycle"] = "disabled"
            spec["run"] = lambda args: ToolResult.error(
                "Error: internal workspace API", code="policy_denied", retryable=False
            )
            spec.pop("run_with_context", None)
            spec.pop("parallel_run", None)
        elif name in WORKSPACE_TOOLS:
            spec["execution_tier"] = "linux_sandbox"
            spec["run"] = lambda args, name=name: execute_workspace_tool(context, name, args)
            spec["run_with_context"] = lambda ctx, args, name=name: execute_workspace_tool(
                ctx, name, args, mark_read=False
            )
            spec["parallel_run"] = spec["run"]
        elif sandbox_tool_access(name) is not SandboxToolAccess.DENIED:
            # A name or metadata flag is not authority to run an injected host function.
            spec["run"] = lambda args, name=name: execute_canonical_tool(context, name, args)
            spec["run_with_context"] = lambda ctx, args, name=name: execute_canonical_tool(
                ctx, name, args
            )
            spec["parallel_run"] = spec["run"]
            spec["execution_mode"] = "thread"
            spec["isolated_read"] = False
            spec["terminal"] = name == "finish_repair"
            if name in {"run_shell", "quick_test"}:
                spec["execution_tier"] = "linux_sandbox"
        elif sandbox_tool_access(name) is SandboxToolAccess.DENIED:
            spec["lifecycle"] = "disabled"
            spec["run"] = lambda args: ToolResult.error(
                "Error: tool not migrated to workspace sandbox",
                code="policy_denied",
                retryable=False,
            )
            spec.pop("run_with_context", None)
            spec.pop("parallel_run", None)
    return registry


def execute_canonical_tool(context, name, args):
    from agent_runtime.tools import tool_expand_observation, tool_quick_test, tool_run_shell

    from .permissions import authorize_trusted, permission_service

    if name == "run_shell":
        return tool_run_shell(context, args)
    if name == "quick_test":
        return tool_quick_test(context, args)
    handler = context.sandbox_trusted_handlers.get(name)
    if name == "expand_lock" and handler is not None:
        return handler(context, args)  # The scope-expansion handler owns its permission receipt.
    rejection, permission = authorize_trusted(context, name, args)
    if rejection is not None:
        return rejection
    handlers = {
        "expand_observation": lambda: tool_expand_observation(context, args),
    }
    try:
        if name in handlers:
            result = handlers[name]()
        elif handler is not None:
            result = handler(context, args)
        else:
            result = ToolResult.error(
                "Error: trusted tool handler not registered", code="policy_denied", retryable=False
            )
    except Exception:
        permission_service(context).outcome(permission, "error")
        raise
    permission_service(context).outcome(permission, str(result.status))
    result.metadata["sandbox_permission"] = permission
    result.metadata["execution_tier"] = "trusted_host"
    result.receipt["permission"] = permission
    return result
