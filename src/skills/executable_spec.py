"""可执行 Skill 规格（与策略 YAML SkillSpec 并存）。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
Lifecycle = Literal["draft", "experimental", "active", "deprecated", "retired"]


class ExecutableSkillSpec(BaseModel):
    """可执行 Skill：触发、Schema、工具白名单与生命周期。"""

    name: str
    version: str = "1.0.0"
    description: str = ""
    positive_triggers: list[str] = Field(default_factory=list)
    negative_triggers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    prototypes: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    completion_evidence: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    requires_read_before_write: bool = False
    fallback: str = "none"
    lifecycle: Lifecycle = "active"
    source: str = "builtin_verified"
    trust_level: Literal["verified", "trusted", "untrusted"] = "verified"
    scope: Literal["builtin", "workspace", "user", "remote"] = "builtin"
    side_effect_level: Literal["none", "local_write", "remote_write", "destructive"] = "none"
    budget: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not SKILL_NAME_PATTERN.match(cleaned):
            raise ValueError("name must match ^[a-z][a-z0-9_]*$")
        return cleaned

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        cleaned = value.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", cleaned):
            raise ValueError("version must be SemVer")
        return cleaned

    @field_validator("positive_triggers", "negative_triggers")
    @classmethod
    def validate_regex_list(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            pat = str(raw).strip()
            if not pat:
                continue
            try:
                re.compile(pat)
            except re.error as exc:
                raise ValueError(f"invalid trigger regex {pat!r}: {exc}") from exc
            out.append(pat)
        return out

    def is_active(self) -> bool:
        return self.lifecycle == "active"
