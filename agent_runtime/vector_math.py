"""Dependency-free similarity for finite, one-dimensional embedding vectors."""

from __future__ import annotations

import math
from typing import Any


def _unit_vector(value: Any) -> list[float]:
    if isinstance(value, str | bytes):
        return []
    try:
        vector = [float(x) for x in value]
    except (TypeError, ValueError, OverflowError):
        return []
    if not vector or not all(math.isfinite(x) for x in vector):
        return []
    scale = max(abs(x) for x in vector)
    if scale == 0:
        return []
    scaled = [x / scale for x in vector]
    norm = math.sqrt(math.fsum(x * x for x in scaled))
    return [x / norm for x in scaled]


def cosine_similarity(a: Any, b: Any) -> float:
    """Return zero for unusable vectors; scale before normalization to avoid overflow."""
    va, vb = _unit_vector(a), _unit_vector(b)
    if not va or len(va) != len(vb):
        return 0.0
    return max(-1.0, min(1.0, math.fsum(x * y for x, y in zip(va, vb))))
