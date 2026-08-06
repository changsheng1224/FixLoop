"""可执行 Skill Router：规则 → 关键词/Embedding → 低 Margin Fallback（可选 LLM）。"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from src.skills.executable_spec import ExecutableSkillSpec
from src.skills.registry import ROUTER_VERSION, SkillRegistry, get_default_executable_registry

EmbedFn = Callable[[str], Any]
LlmPickFn = Callable[[str, list[dict[str, Any]]], str | None]

MARGIN_TAU = 0.08
SCORE_FLOOR = 0.45
RULE_SCORE = 0.95


@dataclass
class CandidateScore:
    name: str
    score: float
    tier: str  # rule | keyword | embed | llm | excluded


@dataclass
class RouteDecision:
    selected: str | None
    selection_reason: str
    margin: float
    candidates: list[CandidateScore] = field(default_factory=list)
    skill_version: str | None = None
    low_margin: bool = False
    fallback: bool = False
    router_version: str = ROUTER_VERSION
    switched_from: str | None = None

    def to_trace_payload(self) -> dict[str, Any]:
        return {
            "event_kind": "executable_skill_route",
            "selected": self.selected,
            "selection_reason": self.selection_reason,
            "margin": round(self.margin, 4),
            "skill_version": self.skill_version,
            "router_version": self.router_version,
            "low_margin": self.low_margin,
            "fallback": self.fallback,
            "switched_from": self.switched_from,
            "candidates": [asdict(c) for c in self.candidates],
        }


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "from",
        "with",
        "is",
        "are",
        "was",
        "be",
        "this",
        "that",
        "it",
        "as",
        "at",
        "by",
        "into",
        "please",
        "what",
        "where",
        "how",
        "when",
        "who",
        "why",
        "me",
        "my",
        "we",
        "you",
        "any",
        "all",
        "do",
        "does",
        "did",
    }
)


def _tokenize(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-zA-Z_][\w.-]*", (text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    }


def _keyword_score(text: str, spec: ExecutableSkillSpec) -> float:
    tokens = _tokenize(text)
    if not tokens:
        return 0.0
    keys: set[str] = set()
    for item in spec.keywords + spec.prototypes:
        keys |= _tokenize(item)
    if not keys:
        return 0.0
    hit = len(tokens & keys)
    return min(0.92, hit / max(4.0, math.sqrt(len(keys))))


def _cosine(a: Any, b: Any) -> float:
    try:
        import numpy as np

        va = np.asarray(a, dtype=float).ravel()
        vb = np.asarray(b, dtype=float).ravel()
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))
    except Exception:
        va = list(a)
        vb = list(b)
        if len(va) != len(vb) or not va:
            return 0.0
        dot = sum(x * y for x, y in zip(va, vb))
        na = sum(x * x for x in va) ** 0.5
        nb = sum(y * y for y in vb) ** 0.5
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)


def _embed_score(text: str, spec: ExecutableSkillSpec, embed_fn: EmbedFn) -> float:
    try:
        q = embed_fn(text)
        best = 0.0
        for proto in spec.prototypes or spec.keywords:
            best = max(best, _cosine(q, embed_fn(proto)))
        return float(best)
    except Exception:
        return 0.0


def _rule_hit(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if re.search(pat, text or ""):
            return True
    return False


class SkillRouter:
    """分级路由。LLM 仅在低 Margin 时可选调用。"""

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        *,
        embed_fn: EmbedFn | None = None,
        llm_pick_fn: LlmPickFn | None = None,
        margin_tau: float = MARGIN_TAU,
        score_floor: float = SCORE_FLOOR,
    ) -> None:
        self.registry = registry or get_default_executable_registry()
        self.embed_fn = embed_fn
        self.llm_pick_fn = llm_pick_fn
        self.margin_tau = margin_tau
        self.score_floor = score_floor

    def score_candidates(self, text: str) -> list[CandidateScore]:
        scored: list[CandidateScore] = []
        for spec in self.registry.list(lifecycle="active"):
            if _rule_hit(text, spec.negative_triggers):
                scored.append(CandidateScore(spec.name, 0.0, "excluded"))
                continue
            kw = _keyword_score(text, spec)
            if _rule_hit(text, spec.positive_triggers):
                # 规则命中为主，关键词作并列打破
                score = min(0.99, RULE_SCORE + 0.04 * kw)
                scored.append(CandidateScore(spec.name, score, "rule"))
                continue
            emb = _embed_score(text, spec, self.embed_fn) if self.embed_fn else 0.0
            if emb >= kw and emb > 0:
                scored.append(CandidateScore(spec.name, emb, "embed"))
            else:
                scored.append(CandidateScore(spec.name, kw, "keyword"))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored

    def route(
        self,
        text: str,
        *,
        previous_selected: str | None = None,
    ) -> RouteDecision:
        all_cands = self.score_candidates(text)
        active = [c for c in all_cands if c.tier != "excluded"]
        active.sort(key=lambda c: c.score, reverse=True)

        if not active or active[0].score < self.score_floor:
            return RouteDecision(
                selected=None,
                selection_reason="fallback",
                margin=0.0,
                candidates=all_cands,
                skill_version=None,
                low_margin=True,
                fallback=True,
                switched_from=previous_selected,
            )

        top = active[0]
        second = active[1].score if len(active) > 1 else 0.0
        margin = top.score - second
        low_margin = margin < self.margin_tau

        if top.tier == "rule" and top.score >= RULE_SCORE - 0.02:
            # 强规则 Top-1：即使第二名也是 rule（靠关键词并列打破）
            return self._decide(
                top.name,
                "rule_short_circuit",
                margin,
                all_cands,
                low_margin=False,
                previous_selected=previous_selected,
            )

        if not low_margin and top.score >= self.score_floor:
            return self._decide(
                top.name,
                "top1_margin",
                margin,
                all_cands,
                low_margin=False,
                previous_selected=previous_selected,
            )

        # 低 Margin：可选 LLM
        if self.llm_pick_fn is not None:
            try:
                pick = self.llm_pick_fn(
                    text,
                    [{"name": c.name, "score": c.score, "tier": c.tier} for c in active[:3]],
                )
                if pick and self.registry.get(pick):
                    return self._decide(
                        pick,
                        "llm_fallback",
                        margin,
                        all_cands,
                        low_margin=True,
                        previous_selected=previous_selected,
                    )
            except Exception:
                pass

        return RouteDecision(
            selected=None,
            selection_reason="fallback",
            margin=round(margin, 4),
            candidates=all_cands,
            skill_version=None,
            low_margin=True,
            fallback=True,
            switched_from=previous_selected,
        )

    def _decide(
        self,
        name: str,
        reason: str,
        margin: float,
        candidates: list[CandidateScore],
        *,
        low_margin: bool,
        previous_selected: str | None,
    ) -> RouteDecision:
        spec = self.registry.get(name)
        switched = previous_selected if previous_selected and previous_selected != name else None
        return RouteDecision(
            selected=name,
            selection_reason=reason,
            margin=round(margin, 4),
            candidates=candidates,
            skill_version=spec.version if spec else None,
            low_margin=low_margin,
            fallback=False,
            switched_from=switched,
        )


def route_executable_skill(text: str, **kwargs: Any) -> RouteDecision:
    allowed = {"registry", "embed_fn", "llm_pick_fn"}
    router_kwargs = {key: value for key, value in kwargs.items() if key in allowed}
    return SkillRouter(**router_kwargs).route(text)
