"""Global and per-role resource reservations with backpressure decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class BudgetLimits:
    tokens: float = 0.0
    tools: float = 0.0
    wall_seconds: float = 0.0
    concurrency: int = 0


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str
    reservation_id: str = ""
    remaining: dict[str, float] = field(default_factory=dict)


class BudgetLedger:
    """Thread-safe reservation ledger; zero limits mean unlimited."""

    def __init__(self, limits: BudgetLimits | None = None):
        self.limits = limits or BudgetLimits()
        self._used: dict[str, float] = {"tokens": 0.0, "tools": 0.0, "wall_seconds": 0.0}
        self._active = 0
        self._reservations: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def reserve(self, *, role: str, costs: dict[str, float], reservation_id: str) -> BudgetDecision:
        with self._lock:
            if reservation_id in self._reservations:
                return BudgetDecision(True, "idempotent", reservation_id, self.remaining())
            limits = self.limits
            for name in ("tokens", "tools", "wall_seconds"):
                limit = float(getattr(limits, name))
                if limit > 0 and self._used[name] + float(costs.get(name, 0.0)) > limit:
                    return BudgetDecision(
                        False, f"{name}_budget_exhausted", remaining=self.remaining()
                    )
            if limits.concurrency > 0 and self._active >= limits.concurrency:
                return BudgetDecision(False, "concurrency_limit", remaining=self.remaining())
            normalized = {name: max(0.0, float(costs.get(name, 0.0))) for name in self._used}
            for name, amount in normalized.items():
                self._used[name] += amount
            self._active += 1
            self._reservations[reservation_id] = {"role": role, "costs": normalized}
            return BudgetDecision(True, "reserved", reservation_id, self.remaining())

    def release(self, reservation_id: str, *, actual_costs: dict[str, float] | None = None) -> bool:
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                return False
            reserved = reservation["costs"]
            actual = actual_costs or reserved
            for name in self._used:
                self._used[name] = max(
                    0.0, self._used[name] - reserved[name] + max(0.0, float(actual.get(name, 0.0)))
                )
            self._active = max(0, self._active - 1)
            return True

    def remaining(self) -> dict[str, float]:
        return {
            "tokens": max(0.0, self.limits.tokens - self._used["tokens"])
            if self.limits.tokens > 0
            else float("inf"),
            "tools": max(0.0, self.limits.tools - self._used["tools"])
            if self.limits.tools > 0
            else float("inf"),
            "wall_seconds": max(0.0, self.limits.wall_seconds - self._used["wall_seconds"])
            if self.limits.wall_seconds > 0
            else float("inf"),
            "concurrency": max(0, self.limits.concurrency - self._active)
            if self.limits.concurrency > 0
            else float("inf"),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "limits": self.limits.__dict__.copy(),
                "used": dict(self._used),
                "active": self._active,
                "reservations": {key: dict(value) for key, value in self._reservations.items()},
            }
