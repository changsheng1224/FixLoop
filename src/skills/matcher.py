"""Deterministic Skill matching for repair issues."""

from __future__ import annotations

from src.skills.catalog import SkillCatalog, get_default_catalog
from src.skills.models import MatchedSkill, SkillSpec


def _rank_key(spec: SkillSpec) -> tuple[int, int, str]:
    return (-spec.priority, -len(spec.trigger_pattern), spec.name)


def match_skill(
    issue: str,
    *,
    language: str = "python",
    catalog: SkillCatalog | None = None,
) -> MatchedSkill | None:
    """Return the best matching Skill for *issue*, or ``None``."""
    registry = catalog or get_default_catalog()
    candidates = [
        spec
        for spec in registry.skills
        if spec.language == language and spec.matches(issue)
    ]
    if not candidates:
        return None
    candidates.sort(key=_rank_key)
    best = candidates[0]
    return MatchedSkill.from_spec(best, candidates_count=len(candidates))
