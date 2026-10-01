"""Audited tool access classes for the fixed WSL sandbox profile."""

from __future__ import annotations

from enum import Enum


class SandboxToolAccess(str, Enum):
    TRUSTED_DATA = "trusted_data"
    TRUSTED_CONTROL = "trusted_control"
    TRUSTED_WRITE = "trusted_write"
    SANDBOX_COMMAND = "sandbox_command"
    DENIED = "denied"


_ACCESS = {
    **dict.fromkeys(
        (
            "read_file",
            "list_files",
            "grep",
            "search",
            "code_lookup",
            "code_relations",
            "inspect_file",
            "find_test",
            "ast_parse",
            "stack_parse",
            "java_ast_parse",
            "java_stack_parse",
            "expand_observation",
        ),
        SandboxToolAccess.TRUSTED_DATA,
    ),
    **dict.fromkeys(
        ("finish_repair", "expand_lock", "delegate_exploration", "collect_exploration"),
        SandboxToolAccess.TRUSTED_CONTROL,
    ),
    **dict.fromkeys(("write_file", "patch_file", "apply_patch"), SandboxToolAccess.TRUSTED_WRITE),
    **dict.fromkeys(("quick_test", "run_shell"), SandboxToolAccess.SANDBOX_COMMAND),
}


def sandbox_tool_access(name: str) -> SandboxToolAccess:
    return _ACCESS.get(name, SandboxToolAccess.DENIED)
