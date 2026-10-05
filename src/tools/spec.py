"""Repair-domain tool policies; generic contracts live in Layer 1."""

from dataclasses import replace

from agent_runtime import tool_spec

_READ_PHASES = frozenset({"context", "localization", "patch", "verify", "verification"})
_PATCHER = frozenset({"patcher"})
_READERS = frozenset({"*"})


def _spec(name: str, roles: frozenset[str], **kwargs) -> tool_spec.ToolSpec:
    return tool_spec.ToolSpec(name=name, roles=roles, phases=_READ_PHASES, **kwargs)


def default_repair_tool_registry(*, sandbox_mode: bool = False) -> tool_spec.ToolRegistry:
    specs = [
        _spec("read_file", _READERS, capabilities=frozenset({"filesystem.read"})),
        _spec("grep", _READERS, capabilities=frozenset({"code.search"})),
        _spec("code_lookup", _READERS, capabilities=frozenset({"code.lsp"})),
        _spec("code_relations", _READERS, capabilities=frozenset({"code.relations"})),
        _spec("list_files", _READERS, capabilities=frozenset({"filesystem.list"})),
        _spec("inspect_file", _PATCHER, capabilities=frozenset({"filesystem.read", "code.ast"})),
        _spec("find_test", _PATCHER, capabilities=frozenset({"test.discover"})),
        _spec("git_blame", _PATCHER, capabilities=frozenset({"git.read"})),
        _spec("git_diff", _PATCHER, capabilities=frozenset({"git.read"})),
        _spec("ast_parse", _PATCHER, capabilities=frozenset({"code.ast"})),
        _spec("stack_parse", _PATCHER, capabilities=frozenset({"trace.parse"})),
        _spec("java_ast_parse", _PATCHER, capabilities=frozenset({"code.ast"})),
        _spec("java_stack_parse", _PATCHER, capabilities=frozenset({"trace.parse"})),
        _spec(
            "write_file",
            _PATCHER,
            budget_group="write",
            side_effect="write",
            risk_level="high",
            requires_approval=True,
            replay_policy="never_replay",
            capabilities=frozenset({"filesystem.write"}),
        ),
        _spec(
            "patch_file",
            _PATCHER,
            budget_group="write",
            side_effect="write",
            risk_level="high",
            requires_approval=True,
            replay_policy="never_replay",
            capabilities=frozenset({"filesystem.write"}),
        ),
        tool_spec.ToolSpec(
            "apply_patch",
            roles=_PATCHER,
            phases=frozenset({"patch"}),
            modes=frozenset({"repair", "refactor"}),
            budget_group="write",
            side_effect="write",
            replay_policy="never_replay",
            risk_level="high",
            requires_approval=True,
            capabilities=frozenset({"filesystem.write", "patch.apply"}),
            requires_evidence=True,
            requires_read_before_write=True,
        ),
        tool_spec.ToolSpec(
            "finish_repair",
            roles=_PATCHER,
            phases=frozenset({"patch"}),
            budget_group="recovery",
            replay_policy="never_replay",
            capabilities=frozenset({"repair.terminate"}),
            terminal=True,
        ),
        tool_spec.ToolSpec(
            "expand_lock",
            roles=_PATCHER,
            phases=frozenset({"patch"}),
            budget_group="recovery",
            capabilities=frozenset({"policy.edit_scope"}),
        ),
        tool_spec.ToolSpec(
            "quick_test",
            roles=frozenset({"patcher", "verifier"}),
            phases=frozenset({"patch", "verify", "verification"}),
            budget_group="verify",
            side_effect="external",
            replay_policy="never_replay" if sandbox_mode else "revalidate",
            risk_level="high" if sandbox_mode else "low",
            requires_approval=sandbox_mode,
            capabilities=frozenset({"test.run"}),
        ),
        tool_spec.ToolSpec(
            "run_shell",
            roles=_PATCHER if sandbox_mode else frozenset(),
            phases=frozenset({"patch"}) if sandbox_mode else _READ_PHASES,
            lifecycle="active" if sandbox_mode else "disabled",
            budget_group="verify",
            side_effect="external",
            replay_policy="never_replay",
            risk_level="high",
            requires_approval=True,
        ),
        tool_spec.ToolSpec(
            "sandbox_build",
            roles=frozenset({"verifier"}),
            phases=frozenset({"verify"}),
            budget_group="verify",
            side_effect="external",
        ),
        tool_spec.ToolSpec(
            "sandbox_test",
            roles=frozenset({"verifier"}),
            phases=frozenset({"verify"}),
            budget_group="verify",
            side_effect="external",
        ),
        tool_spec.ToolSpec(
            "sandbox_verify",
            roles=frozenset({"verifier"}),
            phases=frozenset({"verify"}),
            budget_group="verify",
            side_effect="external",
        ),
    ]
    if sandbox_mode:
        from agent_runtime.linux_sandbox.tool_policy import (
            SandboxToolAccess,
            sandbox_tool_access,
        )

        specs = [
            replace(spec, lifecycle="disabled", roles=frozenset())
            if sandbox_tool_access(spec.name) is SandboxToolAccess.DENIED
            else spec
            for spec in specs
        ]
    return tool_spec.ToolRegistry(specs)
