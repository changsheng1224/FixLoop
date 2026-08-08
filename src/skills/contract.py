"""Canonical contracts shared by guidance and executable Skills.

The contract is intentionally policy-only: a Skill describes requested
capabilities, while the runtime remains the authority that admits execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SkillKind(StrEnum):
    GUIDANCE = "guidance"
    EXECUTABLE = "executable"
    HYBRID = "hybrid"


class SkillLifecycle(StrEnum):
    DRAFT = "draft"
    EXPERIMENTAL = "experimental"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class SkillTrust(StrEnum):
    VERIFIED = "verified"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class SkillScope(StrEnum):
    BUILTIN = "builtin"
    USER = "user"
    WORKSPACE = "workspace"
    REMOTE = "remote"


class SideEffectLevel(StrEnum):
    NONE = "none"
    LOCAL_WRITE = "local_write"
    REMOTE_WRITE = "remote_write"
    DESTRUCTIVE = "destructive"


class SkillBudgetProfile(BaseModel):
    max_tool_calls: int = Field(default=8, ge=0, le=1000)
    max_retries: int = Field(default=0, ge=0, le=5)
    timeout_s: float = Field(default=30.0, gt=0, le=3600)
    max_output_chars: int = Field(default=50_000, ge=1, le=5_000_000)


class CanonicalSkillSpec(BaseModel):
    """One governed identity for prompt guidance and executable capability."""

    name: str
    version: str = "1.0.0"
    kind: SkillKind = SkillKind.EXECUTABLE
    description: str = ""
    source: str = "builtin_verified"
    trust_level: SkillTrust = SkillTrust.VERIFIED
    scope: SkillScope = SkillScope.BUILTIN
    lifecycle: SkillLifecycle = SkillLifecycle.ACTIVE
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    completion_evidence: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    requires_read_before_write: bool = False
    guidance: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    side_effect_level: SideEffectLevel = SideEffectLevel.NONE
    budget: SkillBudgetProfile = Field(default_factory=SkillBudgetProfile)
    fallback: str = "none"
    content_hash: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
            raise ValueError("name must match ^[a-z][a-z0-9_]*$")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", value):
            raise ValueError("version must be SemVer")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = {"builtin_verified", "workspace_local", "user_provided", "remote_untrusted"}
        if value not in allowed:
            raise ValueError(f"source must be one of: {', '.join(sorted(allowed))}")
        return value

    def stable_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def with_hash(self) -> CanonicalSkillSpec:
        return self.model_copy(update={"content_hash": self.content_hash or self.stable_hash()})

    def permits_new_invocation(self) -> bool:
        return self.lifecycle in {SkillLifecycle.EXPERIMENTAL, SkillLifecycle.ACTIVE}

    def fail_closed(self) -> bool:
        return (
            self.side_effect_level is not SideEffectLevel.NONE
            or self.trust_level is SkillTrust.UNTRUSTED
        )


def canonical_from_executable(spec: Any) -> CanonicalSkillSpec:
    """Adapt the legacy executable specification without changing its YAML."""
    side_effect = SideEffectLevel(str(getattr(spec, "side_effect_level", "none")))
    tools = list(getattr(spec, "allowed_tools", []) or [])
    if side_effect is SideEffectLevel.NONE and any(
        name.startswith("github_create") for name in tools
    ):
        side_effect = SideEffectLevel.REMOTE_WRITE
    elif side_effect is SideEffectLevel.NONE and any(
        name in {"apply_patch", "patch_file", "write_file"} for name in tools
    ):
        side_effect = SideEffectLevel.LOCAL_WRITE
    lifecycle = str(getattr(spec, "lifecycle", "active"))
    return CanonicalSkillSpec(
        name=spec.name,
        version=spec.version,
        kind=SkillKind.EXECUTABLE,
        description=spec.description,
        source=str(getattr(spec, "source", "builtin_verified")),
        trust_level=SkillTrust(str(getattr(spec, "trust_level", "verified"))),
        scope=SkillScope(str(getattr(spec, "scope", "builtin"))),
        lifecycle=SkillLifecycle(lifecycle),
        input_schema=dict(spec.input_schema),
        output_schema=dict(spec.output_schema),
        allowed_tools=list(spec.allowed_tools),
        completion_evidence=list(spec.completion_evidence),
        preconditions=list(getattr(spec, "preconditions", []) or []),
        postconditions=list(getattr(spec, "postconditions", []) or []),
        requires_read_before_write=bool(
            getattr(spec, "requires_read_before_write", False)
            or side_effect is SideEffectLevel.LOCAL_WRITE
        ),
        side_effect_level=side_effect,
        budget=SkillBudgetProfile.model_validate(getattr(spec, "budget", {}) or {}),
        fallback=spec.fallback,
    ).with_hash()


def canonical_from_guidance(spec: Any) -> CanonicalSkillSpec:
    """Adapt the original prompt-guidance Skill to the governed identity."""
    version = str(getattr(spec, "version", "1") or "1")
    if version.isdigit():
        version = f"{version}.0.0"
    scope_map = {
        "workspace": SkillScope.WORKSPACE,
        "user": SkillScope.USER,
        "remote": SkillScope.REMOTE,
    }
    return CanonicalSkillSpec(
        name=spec.name,
        version=version,
        kind=SkillKind.GUIDANCE,
        source=str(getattr(spec, "source", "builtin_verified")),
        trust_level=SkillTrust(str(getattr(spec, "trust_level", "verified"))),
        scope=scope_map.get(str(getattr(spec, "scope", "workspace")), SkillScope.BUILTIN),
        allowed_tools=list(getattr(spec, "suggested_tools", []) or []),
        guidance=list(getattr(spec, "guidance", []) or []),
        avoid=list(getattr(spec, "avoid", []) or []),
    ).with_hash()


def validate_json_contract(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate the JSON-Schema subset used by built-in Skills.

    Keeping this small avoids introducing a second schema dependency. Unknown
    keywords are ignored; type, required, properties, items, enum and
    additionalProperties are enforced.
    """
    if not schema:
        return []
    issues: list[str] = []
    expected = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if expected in type_map:
        valid = isinstance(value, type_map[expected])
        if expected in {"integer", "number"} and isinstance(value, bool):
            valid = False
        if not valid:
            return [f"{path}: expected {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        issues.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                issues.append(f"{path}.{name}: required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    issues.append(f"{path}.{name}: additional property not allowed")
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, dict):
                issues.extend(validate_json_contract(value[name], child_schema, f"{path}.{name}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            issues.extend(validate_json_contract(item, schema["items"], f"{path}[{index}]"))
    return issues


def resolve_evidence(output: dict[str, Any], dotted_path: str) -> tuple[bool, Any]:
    value: Any = output
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return value is not None, value
