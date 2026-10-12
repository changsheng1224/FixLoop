"""Trusted local memory identity, checked before retrieval or mutation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agent_runtime.state_root import managed_state_base, project_id, trusted_state_root


@dataclass(frozen=True)
class MemoryAccessContext:
    project_id: str
    task_id: str
    user_id: str = "local"
    shared_read: bool = False

    def allows(
        self, record: dict, *, write: bool = False, version: str = "", check_version: bool = True,
    ) -> bool:
        if (
            not self.project_id or not self.user_id
            or record.get("project_id") != self.project_id
            or record.get("user_id") != self.user_id
            or (write and self.shared_read)
        ):
            return False
        scope = record.get("scope")
        if self.shared_read:
            if (
                scope not in {"repository", "repository_version"}
                or record.get("status") != "durable"
            ):
                return False
        elif scope in {"run", "task"}:
            if not self.task_id or record.get("task_id") != self.task_id:
                return False
        elif scope not in {"repository", "repository_version", "user"}:
            return False
        if check_version and not write and scope == "repository_version":
            return bool(version and record.get("repo_fingerprint") == version)
        return True

    def memory_id(self, original: str, scope: str) -> str:
        task = self.task_id if scope in {"run", "task"} else ""
        return "M-" + hashlib.sha256(
            f"{self.project_id}|{self.user_id}|{task}|{scope}|{original}".encode()
        ).hexdigest()[:24]


def agent_access_context(agent, *, shared_read: bool = False) -> MemoryAccessContext:
    """Session/model claims never select the project or caller principal."""
    context = agent.tool_context
    source = context.project_root or agent._cwd
    binding = context.sandbox_task_binding
    if binding:
        binding.assert_context(context)
    task = binding.task_id if binding else (
        agent._memory_task_id or getattr(agent, "shared_run_id", None) or agent._memory_session_id
    )
    from agent_runtime.linux_sandbox.task_context import checked_id

    checked_id(task)
    access = MemoryAccessContext(project_id(source), task, shared_read=shared_read)
    agent.session.setdefault("memory", {})["memory_identity"] = {
        "project_id": access.project_id, "user_id": access.user_id, "task_id": access.task_id,
    }
    return access


def agent_governance(agent, *, shared_read: bool = False):
    from pathlib import Path

    from agent_runtime.features.memory.governance import MemoryGovernanceService

    access = agent_access_context(agent, shared_read=shared_read)
    context = agent.tool_context
    project_base = Path(context.embedding_state_base) / "projects" / access.project_id
    root = project_base / "knowledge" if shared_read else trusted_state_root(
        agent._cwd, context.state_root or project_base / "tasks" / access.task_id,
        create=False,
    )
    return MemoryGovernanceService(
        {} if shared_read else agent.session.setdefault("memory", {}),
        repo_root=str(root), access_context=access,
        version_provider=lambda: _content_version(agent),
    )


def _content_version(agent) -> str:
    """Read the bounded live inventory through the active execution boundary."""
    from pathlib import Path

    from agent_runtime.linux_sandbox.workspace_inventory import content_version, inventory

    try:
        if agent.tool_context.sandbox_mode:
            from agent_runtime.linux_sandbox.workspace_state import workspace_versions

            versions = workspace_versions(agent.tool_context)
        else:
            versions = inventory(Path(agent._cwd))
        return content_version(versions)
    except (OSError, ValueError):
        return ""  # Unknown versions cannot admit version-bound facts.


def publish_project_memory(agent, memory_id: str, *, user_confirmed: bool = False) -> str:
    """Explicit application API; never invoked by tools, Dream or finalization."""
    from agent_runtime.features.memory.governance import MemoryGovernanceService, can_promote

    if not user_confirmed:
        raise PermissionError("project memory publication requires explicit confirmation")
    local = agent_governance(agent)
    raw = local.inspect(memory_id)
    if raw is None or raw["scope"] == "user":
        raise PermissionError("memory outside publication boundary")
    # Scope expansion creates a new record with traceable parentage.
    from agent_runtime.features.memory.governance import GovernedMemory

    memory = GovernedMemory(**raw)
    memory.parent_memory_id = memory.memory_id
    memory.scope = "repository" if memory.scope in {"run", "task"} else memory.scope
    if not can_promote(memory, user_confirmed=True)[0]:
        raise PermissionError("memory is conflicted, stale or rejected")
    context = agent.tool_context
    root = managed_state_base(context.project_root, context.embedding_state_base) / "knowledge"
    writer = MemoryGovernanceService(
        {}, repo_root=str(root), access_context=local.access,
        version_provider=lambda: _content_version(agent),
    )
    if writer.store is None:
        raise OSError("project knowledge storage unavailable")
    published = writer.ingest(memory, scope=memory.scope, repo_fingerprint=memory.repo_fingerprint)
    writer.confirm(published.memory_id)
    writer.promote_eligible(user_confirmed=True)
    writer._persist_memory(published.memory_id)
    return published.memory_id
