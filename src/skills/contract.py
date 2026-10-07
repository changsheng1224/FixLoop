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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SkillKind(StrEnum):
    GUIDANCE = "guidance"
    EXECUTABLE = "executable"


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


class SkillSpec(BaseModel):
    """One governed identity for prompt guidance and executable capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str = "python"
    trigger_pattern: str = ""
    priority: int = Field(default=0, ge=0, le=100)
    suggested_tools: list[str] = Field(default_factory=list)
    example_issue: str = ""
    example_patch: str = ""
    positive_triggers: list[str] = Field(default_factory=list)
    negative_triggers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    prototypes: list[str] = Field(default_factory=list)
    name: str
    version: str = "1.0.0"
    kind: SkillKind
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

    @field_validator("trigger_pattern", "positive_triggers", "negative_triggers")
    @classmethod
    def validate_patterns(cls, value):
        for pattern in [value] if isinstance(value, str) else value:
            try:
                re.compile(pattern)
            except re.error as exc:
                # Surface a Pydantic validation error instead of a bare
                # ``re.error`` so callers can handle every contract violation
                # through one exception type.
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return value

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind is SkillKind.GUIDANCE:
            from src.tools.composite import REPAIR_CANONICAL_TOOL_NAMES

            if not self.trigger_pattern or not self.guidance:
                raise ValueError("guidance skills require trigger_pattern and guidance")
            if self.language not in {"python", "javascript", "java"}:
                raise ValueError("unsupported guidance language")
            unknown = set(self.suggested_tools) - set(REPAIR_CANONICAL_TOOL_NAMES)
            if unknown:
                raise ValueError(f"unknown suggested_tools: {sorted(unknown)}")
        return self

    def matches(self, text: str) -> bool:
        return bool(self.trigger_pattern and re.search(self.trigger_pattern, text))

    def stable_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def with_hash(self) -> SkillSpec:
        digest = self.stable_hash()
        if self.content_hash and self.content_hash != digest:
            raise ValueError("skill content_hash does not match its content")
        return self.model_copy(update={"content_hash": digest}, deep=True)

    def permits_new_invocation(self) -> bool:
        return self.lifecycle in {SkillLifecycle.EXPERIMENTAL, SkillLifecycle.ACTIVE}

    def fail_closed(self) -> bool:
        return (
            self.side_effect_level is not SideEffectLevel.NONE
            or self.trust_level is SkillTrust.UNTRUSTED
        )


def validate_json_contract(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Use the same Draft 2020-12 implementation as tool argument validation."""
    from agent_runtime.tool_schema import validate_json_value

    return [
        f"{path}.{error['field']}: {error['message']}"
        for error in validate_json_value(schema, value)
    ]


def resolve_evidence(output: dict[str, Any], dotted_path: str) -> tuple[bool, Any]:
    value: Any = output
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return value is not None, value
