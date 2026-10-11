"""Internal snapshot/rollback consumers. Never resolve or read host source."""

from __future__ import annotations

import json

from .routing import execute_sandbox, sandbox_tool_result
from .workspace_contract import relative_path


def internal_tool(context, name, args=None, *, versions=None):
    execution = execute_sandbox(
        context,
        "workspace",
        (),
        120,
        tool_name=name,
        tool_args=args or {},
        base_versions=versions or {},
    )
    result = sandbox_tool_result(execution, name)
    if name == "workspace_restore":
        with context.sandbox_versions_lock:
            context.sandbox_owned_versions.update(result.metadata.get("committed_versions", {}))
            for path in result.changed_files:
                context.sandbox_workspace_versions.pop(path, None)
    if result.status not in {"success", "no_change"}:
        raise ValueError((result.error_code or "snapshot_unavailable") + ": " + result.content)
    return result


def workspace_versions(context) -> dict[str, str]:
    return internal_tool(context, "workspace_inventory").data


def environment_binding(context) -> dict:
    return internal_tool(context, "workspace_environment").data


def workspace_text(context, path: str) -> str:
    return internal_tool(context, "workspace_text", {"path": relative_path(path)}).data["text"]


class WorkspaceSnapshot(dict):
    """Text contents plus the binary-inclusive version taken at capture time."""

    def __init__(self, files, versions):
        super().__init__(files)
        self.versions = versions


def workspace_snapshot(context, *, include=None) -> WorkspaceSnapshot:
    files, cursor, version = {}, "", None
    versions = workspace_versions(context)
    while True:
        args = {"cursor": cursor}
        if version is not None:
            args["version"] = version
        page = internal_tool(context, "workspace_snapshot", args).data
        if (
            not isinstance(page, dict)
            or set(page) != {"files", "version", "cursor", "complete"}
            or type(page["complete"]) is not bool
            or not isinstance(page["files"], dict)
            or len(page["files"]) > 128
            or not isinstance(page["cursor"], str)
            or len(json.dumps(page, ensure_ascii=True).encode()) > 610000
        ):
            raise ValueError("snapshot_unavailable: invalid snapshot page")
        from .workspace_inventory import content_version

        if page["version"] != content_version(versions):
            raise ValueError("stale_precondition: snapshot version mismatch")
        if page["cursor"]:
            relative_path(page["cursor"])
        for p, text in page["files"].items():
            import hashlib

            if (
                relative_path(p) != p
                or p <= cursor
                or p > page["cursor"]
                or not isinstance(text, str)
                or p in files
                or hashlib.sha256(text.encode()).hexdigest() != versions.get(p)
            ):
                raise ValueError("snapshot_unavailable: invalid snapshot content")
        version = page["version"]
        files.update(
            {p: text for p, text in page["files"].items() if include is None or include(p)}
        )
        if page["complete"]:
            break
        if page["cursor"] <= cursor:
            raise ValueError("snapshot_unavailable: page made no progress")
        cursor = page["cursor"]
    from .workspace_inventory import content_version

    if content_version(versions) != version or workspace_versions(context) != versions:
        raise ValueError("stale_precondition: workspace changed during snapshot")
    return WorkspaceSnapshot(files, versions)


def restore_workspace(context, snapshot):
    if not isinstance(snapshot, WorkspaceSnapshot):
        raise ValueError("stale_precondition: sandbox rollback requires versioned snapshot")
    current = workspace_versions(context)
    changed = {
        p
        for p in current.keys() | snapshot.versions.keys()
        if current.get(p) != snapshot.versions.get(p)
    }
    backend = context.sandbox_backend
    owned = (
        backend.owned_versions()
        if hasattr(backend, "owned_versions")
        else context.sandbox_owned_versions
    )
    files, bases = {}, {}
    # Validate all preconditions before starting any conditional commits.
    for path in sorted(changed):
        relative_path(path)
        if path not in owned or current.get(path) != owned[path]:
            raise ValueError("stale_precondition: rollback would overwrite an external edit")
        if path in snapshot.versions and path not in snapshot:
            raise ValueError("snapshot_unavailable: binary or excluded rollback target")
        files[path] = snapshot.get(path)
        bases[path] = current.get(path)
    batches, batch, used = [], [], 0
    for path in files:
        size = len(json.dumps({path: files[path]}, ensure_ascii=True).encode()) + len(path) + 128
        if size > 900000:
            raise ValueError("snapshot_limit_exceeded: rollback file exceeds request budget")
        if batch and (len(batch) >= 128 or used + size > 900000):
            batches.append(batch)
            batch, used = [], 0
        batch.append(path)
        used += size
    if batch:
        batches.append(batch)
    for batch in batches:
        result = internal_tool(
            context,
            "workspace_restore",
            {"files": {p: files[p] for p in batch}},
            versions={p: bases[p] for p in batch},
        )
        with context.sandbox_versions_lock:
            context.sandbox_owned_versions.update(result.metadata.get("committed_versions", {}))
            for path in result.changed_files:
                context.sandbox_workspace_versions.pop(path, None)
    if workspace_versions(context) != snapshot.versions:
        raise ValueError("rollback_outcome_unconfirmed: workspace changed during rollback")
