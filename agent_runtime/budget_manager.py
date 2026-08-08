"""Unified multi-dimensional runtime budget manager."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BudgetDecision:
    resource: str
    allowed: bool
    used: float
    limit: float
    remaining: float
    action: str = "continue"
    reason: str = ""


class BudgetManager:
    """One ledger for turns, calls, tools and token consumption.

    A limit of zero means unlimited for compatibility with existing config.
    ``reserve`` is intentionally synchronous so a caller cannot forget to
    account for a resource before dispatching an operation.
    """

    ACTIONS = {
        "prompt_tokens": "compress_context",
        "turns": "stop",
        "llm_calls": "stop",
        "tool_calls": "stop",
        "writes": "read_only",
        "verifies": "partial_verify",
        "recoveries": "stop",
    }

    def __init__(self, limits: dict[str, int | float] | None = None):
        self._limits = {str(k): max(0.0, float(v)) for k, v in (limits or {}).items()}
        self._used = {key: 0.0 for key in self._limits}
        self._rejected = {key: 0 for key in self._limits}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config) -> BudgetManager:
        effective = config.effective_budget() if hasattr(config, "effective_budget") else {}
        return cls(effective)

    def reserve(self, resource: str, amount: float = 1.0) -> BudgetDecision:
        key = str(resource)
        value = max(0.0, float(amount))
        with self._lock:
            used = self._used.setdefault(key, 0.0)
            limit = self._limits.setdefault(key, 0.0)
            allowed = limit <= 0 or used + value <= limit
            if allowed:
                self._used[key] = used + value
            else:
                self._rejected[key] = self._rejected.get(key, 0) + 1
            remaining = None if limit <= 0 else max(0.0, limit - self._used[key])
            return BudgetDecision(
                resource=key,
                allowed=allowed,
                used=self._used[key],
                limit=limit,
                remaining=float("inf") if remaining is None else remaining,
                action="continue" if allowed else self.ACTIONS.get(key, "stop"),
                reason="" if allowed else f"{key}_budget_exhausted",
            )

    def reserve_many(self, reservations: dict[str, float]) -> list[BudgetDecision]:
        """Atomically reserve several resources before dispatching an action."""
        normalized = {
            str(resource): max(0.0, float(amount))
            for resource, amount in (reservations or {}).items()
        }
        with self._lock:
            decisions: list[BudgetDecision] = []
            for resource, amount in normalized.items():
                used = self._used.setdefault(resource, 0.0)
                limit = self._limits.setdefault(resource, 0.0)
                allowed = limit <= 0 or used + amount <= limit
                remaining = None if limit <= 0 else max(0.0, limit - used)
                decisions.append(
                    BudgetDecision(
                        resource=resource,
                        allowed=allowed,
                        used=used,
                        limit=limit,
                        remaining=float("inf") if remaining is None else remaining,
                        action="continue" if allowed else self.ACTIONS.get(resource, "stop"),
                        reason="" if allowed else f"{resource}_budget_exhausted",
                    )
                )
            if any(not decision.allowed for decision in decisions):
                for decision in decisions:
                    if not decision.allowed:
                        self._rejected[decision.resource] = (
                            self._rejected.get(decision.resource, 0) + 1
                        )
                return decisions
            for resource, amount in normalized.items():
                self._used[resource] += amount
            return [
                BudgetDecision(
                    resource=decision.resource,
                    allowed=True,
                    used=self._used[decision.resource],
                    limit=decision.limit,
                    remaining=(
                        float("inf")
                        if decision.limit <= 0
                        else max(0.0, decision.limit - self._used[decision.resource])
                    ),
                    action="continue",
                )
                for decision in decisions
            ]

    def backpressure(self) -> list[dict[str, Any]]:
        """Return resources that are exhausted or nearly exhausted."""
        with self._lock:
            pressure = []
            for resource in sorted(set(self._limits) | set(self._used)):
                limit = self._limits.get(resource, 0.0)
                used = self._used.get(resource, 0.0)
                if limit > 0 and used >= limit:
                    pressure.append(
                        {
                            "resource": resource,
                            "used": used,
                            "limit": limit,
                            "action": self.ACTIONS.get(resource, "stop"),
                        }
                    )
            return pressure

    def check(self, resource: str, amount: float = 1.0) -> BudgetDecision:
        """Check capacity without consuming it (useful for multi-resource gates)."""
        key = str(resource)
        value = max(0.0, float(amount))
        with self._lock:
            used = self._used.setdefault(key, 0.0)
            limit = self._limits.setdefault(key, 0.0)
            allowed = limit <= 0 or used + value <= limit
            remaining = None if limit <= 0 else max(0.0, limit - used)
            return BudgetDecision(
                resource=key,
                allowed=allowed,
                used=used,
                limit=limit,
                remaining=float("inf") if remaining is None else remaining,
                action="continue" if allowed else self.ACTIONS.get(key, "stop"),
                reason="" if allowed else f"{key}_budget_exhausted",
            )

    def record(self, resource: str, amount: float) -> BudgetDecision:
        """Record usage which was not reserved in advance, such as API tokens."""
        return self.reserve(resource, amount)

    def remaining(self, resource: str) -> float | None:
        with self._lock:
            limit = self._limits.get(str(resource), 0.0)
            return None if limit <= 0 else max(0.0, limit - self._used.get(str(resource), 0.0))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "limits": dict(self._limits),
                "used": dict(self._used),
                "rejected": dict(self._rejected),
            }

    def restore(self, snapshot: dict[str, Any] | None) -> None:
        data = snapshot or {}
        with self._lock:
            for key, value in (data.get("limits") or {}).items():
                self._limits[str(key)] = max(0.0, float(value or 0))
            for key, value in (data.get("used") or {}).items():
                self._used[str(key)] = max(0.0, float(value or 0))
            for key, value in (data.get("rejected") or {}).items():
                self._rejected[str(key)] = max(0, int(value or 0))

    def summary(self) -> dict[str, dict[str, float | int | str]]:
        with self._lock:
            result = {}
            for key in sorted(set(self._limits) | set(self._used)):
                limit = self._limits.get(key, 0.0)
                result[key] = {
                    "used": self._used.get(key, 0.0),
                    "limit": limit,
                    "remaining": None if limit <= 0 else max(0.0, limit - self._used.get(key, 0.0)),
                    "rejected": self._rejected.get(key, 0),
                    "exhaustion_action": self.ACTIONS.get(key, "stop"),
                }
            return result

    def decision_payload(self) -> dict[str, Any]:
        return {"resources": self.summary()}


__all__ = ["BudgetDecision", "BudgetManager"]
