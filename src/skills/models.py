"""Skill YAML schema and match result types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from src.tools.composite import REPAIR_CANONICAL_TOOL_NAMES

ALLOWED_SKILL_LANGUAGES = frozenset({"python", "javascript"})
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
KNOWN_SKILL_TOOLS = frozenset(REPAIR_CANONICAL_TOOL_NAMES)


class SkillSpec(BaseModel):
    """Validated Skill definition loaded from YAML."""

    name: str
    language: str = "python"
    trigger_pattern: str
    priority: int = Field(default=0, ge=0, le=100)
    suggested_tools: list[str] = Field(default_factory=list)
    example_issue: str = ""
    guidance: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    example_patch: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must be non-empty")
        if not SKILL_NAME_PATTERN.match(cleaned):
            raise ValueError("name must match ^[a-z][a-z0-9_]*$")
        return cleaned

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in ALLOWED_SKILL_LANGUAGES:
            allowed = ", ".join(sorted(ALLOWED_SKILL_LANGUAGES))
            raise ValueError(f"language must be one of: {allowed}")
        return cleaned

    @field_validator("trigger_pattern")
    @classmethod
    def validate_trigger_pattern(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("trigger_pattern must be non-empty")
        try:
            re.compile(cleaned)
        except re.error as exc:
            raise ValueError(str(exc)) from exc
        return cleaned

    @field_validator("example_issue", "example_patch")
    @classmethod
    def strip_text_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("guidance", "avoid")
    @classmethod
    def strip_list_items(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @field_validator("guidance")
    @classmethod
    def validate_guidance_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("guidance must contain at least one item")
        return value

    @field_validator("suggested_tools")
    @classmethod
    def validate_suggested_tools(cls, value: list[str]) -> list[str]:
        unknown = [tool for tool in value if tool not in KNOWN_SKILL_TOOLS]
        if unknown:
            allowed = ", ".join(REPAIR_CANONICAL_TOOL_NAMES)
            raise ValueError(
                f"unknown suggested_tools: {unknown}; known tools: {allowed}"
            )
        return value

    def matches(self, text: str) -> bool:
        return bool(re.search(self.trigger_pattern, text))


@dataclass(frozen=True)
class MatchedSkill:
    """Deterministic match result for one issue."""

    name: str
    language: str
    trigger_pattern: str
    priority: int
    suggested_tools: list[str] = field(default_factory=list)
    example_issue: str = ""
    guidance: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    example_patch: str = ""
    candidates_count: int = 1

    @classmethod
    def from_spec(cls, spec: SkillSpec, *, candidates_count: int = 1) -> "MatchedSkill":
        return cls(
            name=spec.name,
            language=spec.language,
            trigger_pattern=spec.trigger_pattern,
            priority=spec.priority,
            suggested_tools=list(spec.suggested_tools),
            example_issue=spec.example_issue,
            guidance=list(spec.guidance),
            avoid=list(spec.avoid),
            example_patch=spec.example_patch,
            candidates_count=candidates_count,
        )

    def to_trace_payload(self) -> dict:
        return {
            "matched_skill": self.name,
            "trigger_pattern": self.trigger_pattern,
            "priority": self.priority,
            "suggested_tools": list(self.suggested_tools),
            "candidates_count": self.candidates_count,
        }

    def apply_to_plan(self, plan) -> None:
        """Write matched skill fields onto a ``RepairPlan`` (in-place)."""
        plan.skill.matched_skill = self.name
        plan.skill.suggested_tools = list(self.suggested_tools)
        plan.skill.example_issue = self.example_issue
        plan.skill.guidance = list(self.guidance)
        plan.skill.avoid = list(self.avoid)
        plan.skill.example_patch = self.example_patch
