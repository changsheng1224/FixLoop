"""Convert a benchmark instance to the public repair request only."""

from __future__ import annotations

from src.benchmark.swebench.types import SweInstance


def instance_to_issue(instance: SweInstance) -> str:
    """Evaluation metadata belongs to the independent harness, never repair input."""
    return instance.problem_statement.strip() or "(empty problem_statement)"
