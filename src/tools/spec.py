"""Single source of truth for repair tool capabilities and policy metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    protocol_schema: dict[str, Any] = field(default_factory=dict)
    executor: Callable | None = None
    roles: frozenset[str] = frozenset()
    phases: frozenset[str] = frozenset()
    modes: frozenset[str] = frozenset({"repair"})
    budget_group: str = "read"
    timeout_s: float = 30.0
    side_effect: str = "read"
    risk_level: str = "low"
    requires_approval: bool = False
    replay_policy: str = "revalidate"
    trust_level: str = "builtin"
    version: str = "1.0"
    lifecycle: str = "active"
    replacement: str = ""
    capabilities: frozenset[str] = frozenset()
    requires_evidence: bool = False
    requires_read_before_write: bool = False
    provider: str = "local"
    server: str = ""
    execution_mode: str = "thread"
    max_retries: int = 0
    retry_backoff_s: float = 0.1
    rate_limit_per_minute: int = 0
    circuit_breaker_threshold: int = 0

    def json_schema(self) -> dict[str, Any]:
        """Return the canonical provider-neutral JSON Schema."""
        from agent_runtime.tool_schema import schema_to_json

        source = self.protocol_schema or self.input_schema
        return schema_to_json(dict(source or {}))

    def public_view(self) -> dict[str, Any]:
        raw = asdict(self)
        raw.pop("executor", None)
        raw["roles"] = sorted(self.roles)
        raw["phases"] = sorted(self.phases)
        raw["modes"] = sorted(self.modes)
        raw["capabilities"] = sorted(self.capabilities)
        raw["json_schema"] = self.json_schema()
        return raw


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None):
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or spec.name in self._specs:
            raise ValueError(f"duplicate or empty tool name: {spec.name}")
        if spec.lifecycle not in {"experimental", "active", "deprecated", "disabled", "removed"}:
            raise ValueError(f"invalid lifecycle: {spec.lifecycle}")
        if spec.lifecycle == "deprecated" and not spec.replacement:
            raise ValueError(f"deprecated tool requires replacement: {spec.name}")
        if spec.budget_group not in {"read", "write", "verify", "recovery"}:
            raise ValueError(f"invalid budget group: {spec.budget_group}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def visible_to(self, role: str, phase: str = "", mode: str = "repair") -> list[ToolSpec]:
        return [
            spec
            for spec in self._specs.values()
            if spec.lifecycle in {"active", "experimental", "deprecated"}
            and (not spec.roles or "*" in spec.roles or role in spec.roles)
            and (not phase or not spec.phases or phase in spec.phases)
            and mode in spec.modes
        ]

    def capabilities_for(self, role: str, phase: str = "", mode: str = "repair") -> dict:
        visible = self.visible_to(role, phase, mode)
        return {
            "tools": [spec.public_view() for spec in sorted(visible, key=lambda item: item.name)],
            "deprecated": [spec.name for spec in visible if spec.lifecycle == "deprecated"],
            "denied": sorted(set(self._specs) - {spec.name for spec in visible}),
        }

    def bind_execution_tools(self, tools: dict[str, dict]) -> ToolRegistry:
        for name, legacy in tools.items():
            current = self.get(name)
            if current is None:
                self.register(
                    ToolSpec(
                        name=name,
                        description=str(legacy.get("description", "")),
                        input_schema=dict(legacy.get("schema") or {}),
                        protocol_schema=dict(
                            legacy.get("json_schema") or legacy.get("protocol_schema") or {}
                        ),
                        executor=legacy.get("run"),
                        roles=frozenset(legacy.get("roles") or []),
                        phases=frozenset(legacy.get("phases") or []),
                        modes=frozenset(legacy.get("modes") or {"repair"}),
                        budget_group=str(legacy.get("budget_group") or "read"),
                        timeout_s=float(legacy.get("timeout_s") or 30.0),
                        side_effect=str(legacy.get("side_effect") or "read"),
                        risk_level=str(legacy.get("risk_level") or "low"),
                        requires_approval=bool(legacy.get("requires_approval", False)),
                        replay_policy=str(legacy.get("replay_policy") or "revalidate"),
                        trust_level=str(legacy.get("trust_level") or "workspace"),
                        version=str(legacy.get("version") or "1.0"),
                        lifecycle=str(legacy.get("lifecycle") or "active"),
                        capabilities=frozenset(legacy.get("capabilities") or []),
                        provider=str(legacy.get("provider") or "local"),
                        server=str(legacy.get("server") or ""),
                        execution_mode=str(legacy.get("execution_mode") or "thread"),
                        max_retries=max(0, int(legacy.get("max_retries") or 0)),
                        retry_backoff_s=float(legacy.get("retry_backoff_s") or 0.1),
                        rate_limit_per_minute=max(0, int(legacy.get("rate_limit_per_minute") or 0)),
                        circuit_breaker_threshold=max(
                            0, int(legacy.get("circuit_breaker_threshold") or 0)
                        ),
                    )
                )
                continue
            self._specs[name] = replace(
                current,
                description=str(legacy.get("description") or current.description),
                input_schema=dict(legacy.get("schema") or current.input_schema),
                protocol_schema=dict(
                    legacy.get("json_schema")
                    or legacy.get("protocol_schema")
                    or current.protocol_schema
                ),
                executor=legacy.get("run") or current.executor,
                budget_group=str(legacy.get("budget_group") or current.budget_group),
                timeout_s=float(legacy.get("timeout_s") or current.timeout_s),
                side_effect=str(legacy.get("side_effect") or current.side_effect),
                risk_level=str(legacy.get("risk_level") or current.risk_level),
                requires_approval=bool(
                    legacy.get("requires_approval", current.requires_approval)
                ),
                replay_policy=str(legacy.get("replay_policy") or current.replay_policy),
                trust_level=str(legacy.get("trust_level") or current.trust_level),
                version=str(legacy.get("version") or current.version),
                lifecycle=str(legacy.get("lifecycle") or current.lifecycle),
                capabilities=frozenset(legacy.get("capabilities") or current.capabilities),
                provider=str(legacy.get("provider") or current.provider),
                server=str(legacy.get("server") or current.server),
                execution_mode=str(legacy.get("execution_mode") or current.execution_mode),
                max_retries=max(0, int(legacy.get("max_retries") or current.max_retries)),
                retry_backoff_s=float(legacy.get("retry_backoff_s") or current.retry_backoff_s),
                rate_limit_per_minute=max(
                    0, int(legacy.get("rate_limit_per_minute") or current.rate_limit_per_minute)
                ),
                circuit_breaker_threshold=max(
                    0,
                    int(
                        legacy.get("circuit_breaker_threshold")
                        or current.circuit_breaker_threshold
                    ),
                ),
            )
        return self

    def set_roles(self, name: str, roles: set[str] | frozenset[str]) -> None:
        spec = self.get(name)
        if spec is None:
            raise KeyError(name)
        self._specs[name] = replace(spec, roles=frozenset(roles))


_READ_PHASES = frozenset({"context", "localization", "patch", "verify", "verification"})
_PATCHER = frozenset({"patcher"})
_READERS = frozenset({"*"})


def _spec(name: str, roles: frozenset[str], **kwargs) -> ToolSpec:
    return ToolSpec(name=name, roles=roles, phases=_READ_PHASES, **kwargs)


def default_repair_tool_registry() -> ToolRegistry:
    specs = [
        _spec("read_file", _READERS, capabilities=frozenset({"filesystem.read"})),
        _spec("search", _READERS, capabilities=frozenset({"code.search"})),
        _spec("grep", _READERS, capabilities=frozenset({"code.search"})),
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
            "write_file", _PATCHER, budget_group="write", side_effect="write",
            risk_level="high", requires_approval=True,
            replay_policy="never_replay", capabilities=frozenset({"filesystem.write"}),
        ),
        _spec(
            "patch_file", _PATCHER, budget_group="write", side_effect="write",
            risk_level="high", requires_approval=True,
            replay_policy="never_replay", capabilities=frozenset({"filesystem.write"}),
        ),
        ToolSpec(
            "apply_patch", roles=_PATCHER, phases=frozenset({"patch"}),
            modes=frozenset({"repair", "refactor"}), budget_group="write",
            side_effect="write", replay_policy="never_replay",
            risk_level="high", requires_approval=True,
            capabilities=frozenset({"filesystem.write", "patch.apply"}),
            requires_evidence=True, requires_read_before_write=True,
        ),
        ToolSpec(
            "expand_lock", roles=_PATCHER, phases=frozenset({"patch"}),
            budget_group="recovery", capabilities=frozenset({"policy.edit_scope"}),
        ),
        ToolSpec(
            "quick_test", roles=frozenset({"patcher", "verifier"}),
            phases=frozenset({"patch", "verify", "verification"}), budget_group="verify",
            side_effect="external", replay_policy="revalidate",
            capabilities=frozenset({"test.run"}),
        ),
        ToolSpec("run_shell", roles=frozenset(), phases=_READ_PHASES, lifecycle="disabled"),
        ToolSpec(
            "sandbox_build",
            roles=frozenset({"verifier"}),
            phases=frozenset({"verify"}),
            budget_group="verify",
            side_effect="external",
        ),
        ToolSpec(
            "sandbox_test",
            roles=frozenset({"verifier"}),
            phases=frozenset({"verify"}),
            budget_group="verify",
            side_effect="external",
        ),
        ToolSpec(
            "sandbox_verify",
            roles=frozenset({"verifier"}),
            phases=frozenset({"verify"}),
            budget_group="verify",
            side_effect="external",
        ),
    ]
    return ToolRegistry(specs)


def bind_execution_tools(tools: dict[str, dict], registry: ToolRegistry) -> dict[str, dict]:
    """Bind executable projections to canonical ToolSpecs."""
    registry.bind_execution_tools(tools)
    for name, legacy in tools.items():
        spec = registry.get(name)
        if spec is None:
            continue
        legacy.update(
            {
                "version": spec.version,
                "json_schema": spec.json_schema(),
                "lifecycle": spec.lifecycle,
                "roles": sorted(spec.roles),
                "phases": sorted(spec.phases),
                "modes": sorted(spec.modes),
                "budget_group": spec.budget_group,
                "timeout_s": spec.timeout_s,
                "side_effect": spec.side_effect,
                "risk_level": spec.risk_level,
                "requires_approval": spec.requires_approval,
                "replay_policy": spec.replay_policy,
                "trust_level": spec.trust_level,
                "capabilities": sorted(spec.capabilities),
                "provider": spec.provider,
                "server": spec.server,
                "execution_mode": spec.execution_mode,
                "max_retries": spec.max_retries,
                "retry_backoff_s": spec.retry_backoff_s,
                "rate_limit_per_minute": spec.rate_limit_per_minute,
                "circuit_breaker_threshold": spec.circuit_breaker_threshold,
            }
        )
    return tools


def project_tool_specs(specs: list[ToolSpec]) -> dict[str, dict]:
    """Project canonical ToolSpecs into the existing Agent execution mapping."""
    projected: dict[str, dict] = {}
    for spec in specs:
        projected[spec.name] = {
            "schema": dict(spec.input_schema),
            "json_schema": spec.json_schema(),
            "protocol_schema": dict(spec.protocol_schema),
            "description": spec.description,
            "run": spec.executor,
            "risky": spec.side_effect != "read",
            "execution_tier": "host",
            "version": spec.version,
            "lifecycle": spec.lifecycle,
            "roles": sorted(spec.roles),
            "phases": sorted(spec.phases),
            "modes": sorted(spec.modes),
            "budget_group": spec.budget_group,
            "timeout_s": spec.timeout_s,
            "side_effect": spec.side_effect,
            "risk_level": spec.risk_level,
            "requires_approval": spec.requires_approval,
            "replay_policy": spec.replay_policy,
            "trust_level": spec.trust_level,
            "capabilities": sorted(spec.capabilities),
            "provider": spec.provider,
            "server": spec.server,
            "execution_mode": spec.execution_mode,
            "max_retries": spec.max_retries,
            "retry_backoff_s": spec.retry_backoff_s,
            "rate_limit_per_minute": spec.rate_limit_per_minute,
            "circuit_breaker_threshold": spec.circuit_breaker_threshold,
        }
    return projected
