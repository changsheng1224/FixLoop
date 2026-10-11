"""Repair-only terminal and edit-scope tools."""

import json
from dataclasses import dataclass

from agent_runtime.schema_utils import auto_schema
from agent_runtime.tool_result import ToolResult


@dataclass
class ExpandLockArgs:
    """显式扩锁。"""

    path: str = ""


@dataclass
class FinishRepairArgs:
    """End a repair attempt without claiming that a patch was produced."""

    status: str = ""
    reason: str = ""


def tool_expand_lock(context, args: dict) -> ToolResult:
    """显式扩锁：将路径加入 allowed_edit（最多 2 次）；扩后须 read 再写。"""
    raw_path = args.get("path", "")
    if not raw_path:
        return ToolResult.error("Error: 缺少必填参数 path", code="tool_execution_failed")
    lock = context.edit_lock
    if lock is None:
        return ToolResult.error(
            "Error: expand_lock 需要 active edit_lock（patcher_primary）",
            code="tool_execution_failed",
        )
    from agent_runtime.linux_sandbox.workspace_routing import (
        execute_workspace_tool,
        sandbox_enabled,
    )

    if sandbox_enabled(context):
        from agent_runtime.linux_sandbox.permissions import authorize_trusted, permission_service

        rejection, permission = authorize_trusted(context, "expand_lock", args)
        if rejection is not None:
            return rejection
        probe = execute_workspace_tool(context, "read_file", {"path": raw_path, "end": 1},
                                       mark_read=False, record_versions=False)
        if not probe.ok:
            permission_service(context).outcome(permission, str(probe.status))
            return probe
        ok, reason = lock.expand_lock_evidence(raw_path, probe)
        permission_service(context).outcome(permission, "success" if ok else "error")
        probe.metadata["sandbox_permission"] = permission
        probe.receipt["permission"] = permission
        if ok:
            with context.sandbox_versions_lock:
                context.sandbox_workspace_versions.pop(raw_path, None)
    else:
        probe = None
        ok, reason = lock.expand_lock(raw_path)
    if not ok:
        return ToolResult.error(
            f"Error: expand_lock failed ({reason})", code="tool_execution_failed"
        )
    return ToolResult(
        metadata=dict(probe.metadata) if probe is not None else {},
        receipt=dict(probe.receipt) if probe is not None else {},
        content=f"expand_lock ok: {reason}. "
        f"allowed_edit={sorted(lock.allowed_edit)[:12]}. "
        "下一步: read_file 该路径后再 apply_patch/patch_file。"
    )


def tool_finish_repair(args: dict) -> ToolResult:
    """Return an explicit, machine-readable no-patch terminal outcome."""
    status = str(args.get("status") or "").strip().lower()
    reason = str(args.get("reason") or "").strip()
    if status not in {"cannot_patch", "needs_more_context"}:
        return ToolResult.error(
            "Error: finish_repair status 必须是 cannot_patch 或 needs_more_context",
            code="tool_execution_failed",
        )
    if not reason:
        return ToolResult.error(
            "Error: finish_repair reason 不能为空，必须说明当前证据或缺失上下文",
            code="tool_execution_failed",
        )
    return ToolResult(content=json.dumps({"status": status, "reason": reason}, ensure_ascii=False))


def build_repair_control_tools(context):
    registry = {}
    # ---- finish_repair ----
    registry["finish_repair"] = {
        "budget_group": "recovery",
        "schema": auto_schema(FinishRepairArgs),
        "risky": False,
        "terminal": True,
        "execution_tier": "host",
        "description": (
            "结构化结束本次修复且不声称已生成补丁。"
            "status 只能是 cannot_patch 或 needs_more_context；reason 必须说明证据。"
        ),
        "run": tool_finish_repair,
    }

    # ---- expand_lock ----
    registry["expand_lock"] = {
        "budget_group": "recovery",
        "schema": auto_schema(ExpandLockArgs),
        "risky": False,
        "execution_tier": "host",
        "description": "扩锁：将路径加入 allowed_edit（最多2次），随后须 read 再写。参数: path",
        "run": lambda args: tool_expand_lock(context, args),
    }

    return registry
