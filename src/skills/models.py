"""Skill match result projection."""
from __future__ import annotations
from dataclasses import dataclass, field
from src.skills.contract import SkillSpec

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
    source: str = "builtin_verified"
    trust_level: str = "verified"
    scope: str = "workspace"
    version: str = "1.0.0"

    @classmethod
    def from_spec(cls, spec: SkillSpec, *, candidates_count: int = 1) -> MatchedSkill:
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
            source=spec.source,
            trust_level=spec.trust_level,
            scope=spec.scope,
            version=spec.version,
        )

    def to_trace_payload(self) -> dict:
        return {
            "matched_skill": self.name,
            "trigger_pattern": self.trigger_pattern,
            "priority": self.priority,
            "suggested_tools": list(self.suggested_tools),
            "candidates_count": self.candidates_count,
            "confidence": self.confidence,
            "source": self.source,
            "trust_level": self.trust_level,
            "scope": self.scope,
            "version": self.version,
        }

    @property
    def confidence(self) -> float:
        """匹配置信度：priority × 竞争者稀释（0.0–1.0）。"""
        base = self.priority / 100.0
        diversity = 1.0 / max(self.candidates_count, 1)
        return round(base * diversity, 2)

    def apply_to_plan(self, plan) -> None:
        """Write matched skill fields onto a ``RepairPlan`` (in-place)."""
        plan.skill.matched_skill = self.name
        plan.skill.suggested_tools = list(self.suggested_tools)
        plan.skill.example_issue = self.example_issue
        plan.skill.guidance = list(self.guidance)
        plan.skill.avoid = list(self.avoid)
        plan.skill.example_patch = self.example_patch
        plan.skill.confidence = self.confidence
