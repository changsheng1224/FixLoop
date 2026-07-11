"""Skill catalog, matching, and prompt helpers."""

from src.skills.catalog import SkillCatalog, SkillCatalogError, get_default_catalog
from src.skills.matcher import match_skill
from src.skills.models import MatchedSkill, SkillSpec
from src.skills.prompt import SkillHintRole, format_skill_hint, format_skill_hint_block
from src.skills.validate import SkillValidationIssue, SkillValidationReport, validate_directory

__all__ = [
    "MatchedSkill",
    "SkillCatalog",
    "SkillCatalogError",
    "SkillSpec",
    "SkillValidationIssue",
    "SkillValidationReport",
    "SkillHintRole",
    "format_skill_hint",
    "format_skill_hint_block",
    "get_default_catalog",
    "match_skill",
    "validate_directory",
]
