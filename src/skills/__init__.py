"""Skill catalog, matching, and prompt helpers."""

from src.skills.catalog import SkillCatalog, SkillCatalogError, get_default_catalog
from src.skills.executable import run_executable_skill
from src.skills.executable_spec import ExecutableSkillSpec
from src.skills.fallback import (
    SkillFallback,
    apply_skill_fallback,
    resolve_skill_fallback,
    skill_matched_trace_payload,
)
from src.skills.matcher import match_skill
from src.skills.models import MatchedSkill, SkillSpec
from src.skills.prompt import (
    SkillHintRole,
    format_skill_hint,
    format_skill_hint_block,
    format_skill_hint_for_plan,
    format_skill_miss_hint,
)
from src.skills.registry import SkillRegistry, get_default_executable_registry
from src.skills.resolve import resolve_skill_for_plan
from src.skills.router import RouteDecision, SkillRouter, route_executable_skill
from src.skills.validate import SkillValidationIssue, SkillValidationReport, validate_directory

__all__ = [
    "ExecutableSkillSpec",
    "MatchedSkill",
    "RouteDecision",
    "SkillCatalog",
    "SkillCatalogError",
    "SkillFallback",
    "SkillRegistry",
    "SkillRouter",
    "SkillSpec",
    "SkillValidationIssue",
    "SkillValidationReport",
    "SkillHintRole",
    "apply_skill_fallback",
    "get_default_executable_registry",
    "resolve_skill_fallback",
    "resolve_skill_for_plan",
    "route_executable_skill",
    "run_executable_skill",
    "skill_matched_trace_payload",
    "format_skill_hint",
    "format_skill_hint_block",
    "format_skill_hint_for_plan",
    "format_skill_miss_hint",
    "get_default_catalog",
    "match_skill",
    "validate_directory",
]
