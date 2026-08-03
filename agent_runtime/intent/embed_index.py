"""Action-level prototype embedding index for intent matching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from agent_runtime.intent.models import PRIMARY_ACTIONS

EmbedFn = Callable[[str], Any]  # returns vector-like (list/np array)


@dataclass
class EmbedMatch:
    primary: str
    score: float
    margin: float
    top2: str | None = None


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
        # pure python fallback
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


def load_prototypes(path: Path | None = None) -> dict[str, list[str]]:
    path = path or Path(__file__).with_name("prototypes.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, list[str]] = {}
    for key, examples in data.items():
        if key in PRIMARY_ACTIONS and isinstance(examples, list):
            out[key] = [str(x) for x in examples if str(x).strip()]
    return out


class EmbedIndex:
    """Max-pool cosine similarity against action-level prototypes."""

    def __init__(
        self,
        prototypes: dict[str, list[str]] | None = None,
        *,
        embed_fn: EmbedFn | None = None,
    ) -> None:
        self.prototypes = prototypes if prototypes is not None else load_prototypes()
        self.embed_fn = embed_fn
        self._proto_vecs: dict[str, list[Any]] | None = None

    def _ensure_proto_vecs(self) -> dict[str, list[Any]] | None:
        if self.embed_fn is None:
            return None
        if self._proto_vecs is not None:
            return self._proto_vecs
        vecs: dict[str, list[Any]] = {}
        try:
            for primary, examples in self.prototypes.items():
                vecs[primary] = [self.embed_fn(ex) for ex in examples]
        except Exception:
            return None
        self._proto_vecs = vecs
        return vecs

    def match(self, text: str) -> EmbedMatch | None:
        """Return top1 primary by max-pool cosine; None if embed unavailable."""
        if not text or not text.strip():
            return None
        if self.embed_fn is None:
            return None
        proto = self._ensure_proto_vecs()
        if not proto:
            return None
        try:
            q = self.embed_fn(text.strip())
        except Exception:
            return None

        scores: list[tuple[str, float]] = []
        for primary, vecs in proto.items():
            if not vecs:
                continue
            best = max(_cosine(q, v) for v in vecs)
            scores.append((primary, best))
        if not scores:
            return None
        scores.sort(key=lambda x: x[1], reverse=True)
        top1, s1 = scores[0]
        s2 = scores[1][1] if len(scores) > 1 else 0.0
        top2 = scores[1][0] if len(scores) > 1 else None
        return EmbedMatch(primary=top1, score=s1, margin=s1 - s2, top2=top2)
