"""Provider-neutral tool capabilities, schemas and execution registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )
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
    terminal: bool = False

    def json_schema(self) -> dict[str, Any]:
        """Return the canonical provider-neutral JSON Schema."""
        from agent_runtime.tool_schema import schema_to_json

        source = self.input_schema
        return schema_to_json(dict(source or {}))

    def public_view(self) -> dict[str, Any]:
        raw = asdict(self)
        raw.pop("executor", None)
        raw["roles"] = sorted(self.roles)
        raw["phases"] = sorted(self.phases)
        raw["modes"] = sorted(self.modes)
        raw["capabilities"] = sorted(self.capabilities)
        raw["input_schema"] = self.json_schema()
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
        spec.json_schema()
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
        """Bind explicit overrides, preserving false, zero and empty values."""
        from dataclasses import fields

        names = {item.name for item in fields(ToolSpec)} - {"name"}
        aliases = {"schema": "input_schema", "run": "executor"}
        sets = {"roles", "phases", "modes", "capabilities"}
        for name, execution in tools.items():
            updates = {}
            for key, value in execution.items():
                key = aliases.get(key, key)
                if key in names:
                    updates[key] = frozenset(value) if key in sets else value
            current = self.get(name)
            spec = replace(current, **updates) if current else ToolSpec(name=name, **updates)
            spec.json_schema()
            if current is None:
                self.register(spec)
            else:
                # Apply the same invariants as initial registration.
                checked = ToolRegistry([spec])
                self._specs[name] = checked.get(name)
        return self

    def set_roles(self, name: str, roles: set[str] | frozenset[str]) -> None:
        spec = self.get(name)
        if spec is None:
            raise KeyError(name)
        self._specs[name] = replace(spec, roles=frozenset(roles))


def bind_execution_tools(tools: dict[str, dict], registry: ToolRegistry) -> dict[str, dict]:
    """Bind executable projections to canonical ToolSpecs."""
    registry.bind_execution_tools(tools)
    for name, execution in tools.items():
        spec = registry.get(name)
        if spec is None:
            continue
        execution.update(
            {
                "version": spec.version,
                "schema": spec.json_schema(),
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
                "terminal": spec.terminal,
            }
        )
    return tools


def project_tool_specs(specs: list[ToolSpec]) -> dict[str, dict]:
    """Project canonical ToolSpecs into the existing Agent execution mapping."""
    projected: dict[str, dict] = {}
    for spec in specs:
        projected[spec.name] = {
            "schema": spec.json_schema(),
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
            "terminal": spec.terminal,
        }
    return projected
