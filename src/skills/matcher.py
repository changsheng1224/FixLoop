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


def match_skill_semantic(
    issue: str,
    *,
    language: str = "python",
    catalog: SkillCatalog | None = None,
    top_k: int = 5,
) -> MatchedSkill | None:
    """N>100 时先用 semantic embedding 预过滤 top_k，再 regex 精确匹配。

    当前 N=10 时匹配速度足够快，此函数预留为未来扩展。
    """
    registry = catalog or get_default_catalog()
    if len(registry.skills) <= 50:
        return match_skill(issue, language=language, catalog=registry)

    from agent_runtime.features.memory.semantic import SemanticMemory

    sem = SemanticMemory()
    for spec in registry.skills:
        if spec.language == language:
            sem.add({"text": f"{spec.name}: {spec.example_issue[:200]}"})
    if not sem.available:
        return match_skill(issue, language=language, catalog=registry)

    candidates = sem.search(issue, top_k=top_k)
    if not candidates:
        return None
    # regex 确认
    best_matches = [
        spec for spec in registry.skills
        if spec.language == language and spec.matches(issue)
    ]
    if best_matches:
        best_matches.sort(key=_rank_key)
        return MatchedSkill.from_spec(best_matches[0], candidates_count=len(best_matches))
    return None
