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
    """N>50 时先用 semantic embedding 预过滤 top_k，再 regex 精确匹配。

    N≤50 仍走 match_skill 全量 regex（速度足够快）。
    语义索引从 SkillCatalog.embed_index 复用，首次调用时自动构建。
    """
    registry = catalog or get_default_catalog()
    # N≤50 → 全量 regex 足够快
    if registry.skill_count <= 50:
        return match_skill(issue, language=language, catalog=registry)

    # N>50 → semantic pre-filter
    sem = registry.get_embed_index()
    if sem is None or not sem.available:
        # 降级：语义模型不可用 → 回退全量 regex
        return match_skill(issue, language=language, catalog=registry)

    # 向量 top-k 预筛选
    try:
        candidates = sem.search(issue, top_k=top_k)
    except Exception:
        return match_skill(issue, language=language, catalog=registry)

    if not candidates:
        return None

    # regex 精确确认（signal > noise）
    best_matches = [
        spec for spec in registry.skills
        if spec.language == language and spec.matches(issue)
    ]
    if best_matches:
        best_matches.sort(key=_rank_key)
        return MatchedSkill.from_spec(best_matches[0], candidates_count=len(best_matches))
    return None
