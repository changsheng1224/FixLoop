"""Audited tool access classes for the fixed WSL sandbox profile."""

from __future__ import annotations

from enum import StrEnum

from .workspace_contract import WORKSPACE_READ_TOOLS, WORKSPACE_WRITE_TOOLS


class SandboxToolAccess(StrEnum):
    TRUSTED_DATA = "trusted_data"
    TRUSTED_CONTROL = "trusted_control"
    TRUSTED_WRITE = "trusted_write"
    SANDBOX_COMMAND = "sandbox_command"
    SANDBOX_READ = "sandbox_read"
    SANDBOX_WRITE = "sandbox_write"
    DENIED = "denied"


_ACCESS = {
    **dict.fromkeys(WORKSPACE_READ_TOOLS, SandboxToolAccess.SANDBOX_READ),
    **dict.fromkeys(WORKSPACE_WRITE_TOOLS, SandboxToolAccess.SANDBOX_WRITE),
    **dict.fromkeys(
        ("finish_repair", "expand_lock", "expand_observation"),
        SandboxToolAccess.TRUSTED_CONTROL,
    ),
    **dict.fromkeys(("stack_parse", "java_stack_parse"), SandboxToolAccess.TRUSTED_DATA),
    **dict.fromkeys(("quick_test", "run_shell"), SandboxToolAccess.SANDBOX_COMMAND),
}


def sandbox_tool_access(name: str) -> SandboxToolAccess:
    return _ACCESS.get(name, SandboxToolAccess.DENIED)
