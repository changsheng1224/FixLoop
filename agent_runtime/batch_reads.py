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


def recheck_read(result: ToolResult):
    version = result.metadata.get("read_version")
    if result.ok and isinstance(version, dict):
        if version.get("version") != file_version(Path(version["path"])):
            stale_read(result)
    return result
