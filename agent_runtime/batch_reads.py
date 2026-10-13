"""Audited context-explicit reads; version checks never mark edit-lock state."""

from __future__ import annotations

from pathlib import Path

from agent_runtime.code_exploration.io import read_file_result
from agent_runtime.tool_result import ToolResult


def file_version(path: Path):
    try:
        stat = path.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    except OSError:
        return None


def audited_read_file(context, arguments):
    from agent_runtime.linux_sandbox.workspace_routing import (
        execute_workspace_tool,
        sandbox_enabled,
    )

    if sandbox_enabled(context):
        return execute_workspace_tool(context, "read_file", arguments, mark_read=False)
    # The reader still owns path/sensitive-file rejection. A failed version
    # probe adds no authority and never widens the path boundary.
    try:
        path = context.resolve(str(arguments.get("path", "")))
    except ValueError:
        return read_file_result(context, arguments)
    before = file_version(path)
    result = read_file_result(context, arguments)
    after = file_version(path)
    result.metadata["read_version"] = {"path": str(path), "version": before}
    if result.ok and before != after:
        return stale_read(result)
    return result


def stale_read(result: ToolResult):
    result.status = "partial"
    result.error_code = "stale_precondition"
    result.retryable = True
    result.metadata.update({"freshness": "needs_recheck"})
    result.content = (
        "[partial freshness=needs_recheck error_code=stale_precondition] "
        "文件版本已变化，请重新读取后再使用。\n" + result.content
    )
    return result


def normalize_read_result(result: ToolResult):
    retrieval = result.metadata.get("retrieval_result") or {}
    if result.ok and retrieval.get("completeness", "complete_in_scope") != "complete_in_scope":
        result.status = "partial"
        result.error_code = "partial_result"
        result.retryable = True
        result.content = "[partial error_code=partial_result] 读取范围不完整。\n" + result.content
    return result


def recheck_read(result: ToolResult, context=None):
    version = result.metadata.get("read_version")
    if isinstance(version, dict) and version.get("authority") == "sandbox_workspace":
        if not result.ok:
            return result
        from agent_runtime.linux_sandbox.workspace_routing import execute_workspace_tool

        if context is None:
            return stale_read(result)
        path = version["path"]
        expected = result.metadata.get("workspace_versions", {}).get(path)
        probe = execute_workspace_tool(
            context,
            "workspace_source_version",
            {"path": path},
            mark_read=False,
            record_versions=False,
        )
        observed = probe.metadata.get("workspace_versions", {}).get(path)
        # FUSE inode/dev values belong to a single mounted view. Only a content
        # hash can establish freshness across separately mounted calls.
        if not probe.ok or expected is None or expected != observed:
            stale_read(result)
            with context.sandbox_versions_lock:
                if context.sandbox_workspace_versions.get(path) == expected:
                    context.sandbox_workspace_versions.pop(path, None)
        return result
    if result.ok and isinstance(version, dict):
        if version.get("version") != file_version(Path(version["path"])):
            stale_read(result)
    return result
