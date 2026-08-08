"""Deterministic evaluation utilities for Memory Recall and Governance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryEvalCase:
    case_id: str
    query: str
    candidates: tuple[dict[str, Any], ...]
    expected_ids: tuple[str, ...]


def recall_metrics(
    results: list[dict[str, Any]], expected_ids: list[str], k: int = 3
) -> dict[str, float]:
    expected = set(expected_ids)
    ranked = [str(item.get("memory_id", "")) for item in results[: max(0, k)]]
    hits = [item for item in ranked if item in expected]
    first_rank = next((index + 1 for index, item in enumerate(ranked) if item in expected), 0)
    return {
        "recall_at_k": len(set(hits)) / max(len(expected), 1),
        "precision_at_k": len(set(hits)) / max(len(ranked), 1),
        "mrr": 1.0 / first_rank if first_rank else 0.0,
    }


def governance_metrics(
    results: list[dict[str, Any]], *, invalid_ids: set[str], expected_blocked: set[str]
) -> dict[str, float]:
    returned = {str(item.get("memory_id", "")) for item in results}
    blocked = returned.intersection(invalid_ids)
    expected = len(expected_blocked)
    return {
        "invalid_suppression_rate": 1.0 - len(blocked) / max(len(invalid_ids), 1),
        "expected_blocked_rate": len(blocked.intersection(expected_blocked)) / max(expected, 1),
    }


def evaluate_cases(cases: list[MemoryEvalCase], resolver) -> dict[str, Any]:
    """Run deterministic cases against ``resolver(query)``."""
    rows = []
    for case in cases:
        results = list(resolver(case.query))
        metrics = recall_metrics(results, list(case.expected_ids))
        rows.append({"case_id": case.case_id, **metrics})
    count = max(len(rows), 1)
    return {
        "cases": rows,
        "recall_at_k": sum(row["recall_at_k"] for row in rows) / count,
        "precision_at_k": sum(row["precision_at_k"] for row in rows) / count,
        "mrr": sum(row["mrr"] for row in rows) / count,
    }
