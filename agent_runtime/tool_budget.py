"""Grouped tool budgets with reserved write and verification capacity."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum


class ToolBudgetGroup(StrEnum):
    READ = "read"
    WRITE = "write"
    VERIFY = "verify"
    RECOVERY = "recovery"


WRITE_TOOLS = frozenset({"write_file", "patch_file", "apply_patch"})
VERIFY_TOOLS = frozenset(
    {"quick_test", "run_shell", "sandbox_build", "sandbox_test", "sandbox_verify"}
)
RECOVERY_TOOLS = frozenset({"expand_lock"})


def infer_tool_budget_group(tool_name: str, spec: dict | None = None) -> ToolBudgetGroup:
    declared = str((spec or {}).get("budget_group") or "").strip().lower()
    if declared:
        try:
            return ToolBudgetGroup(declared)
        except ValueError:
            pass
    if tool_name in WRITE_TOOLS:
        return ToolBudgetGroup.WRITE
    if tool_name in VERIFY_TOOLS:
        return ToolBudgetGroup.VERIFY
    if tool_name in RECOVERY_TOOLS:
        return ToolBudgetGroup.RECOVERY
    return ToolBudgetGroup.READ


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    group: ToolBudgetGroup
    used: int
    limit: int
    reason: str = ""


class ToolBudgetLedger:
    """Thread-safe independent counters; one group cannot consume another."""

    def __init__(self, limits: dict[str | ToolBudgetGroup, int]):
        self._limits = {
            ToolBudgetGroup(str(key)): max(0, int(value)) for key, value in limits.items()
        }
        for group in ToolBudgetGroup:
            self._limits.setdefault(group, 0)
        self._counts = {group: 0 for group in ToolBudgetGroup}
        self._rejected = {group: 0 for group in ToolBudgetGroup}
        self._lock = threading.Lock()

    def check(self, group: ToolBudgetGroup) -> BudgetDecision:
        with self._lock:
            used = self._counts[group]
            limit = self._limits[group]
            return BudgetDecision(
                allowed=used < limit,
                group=group,
                used=used,
                limit=limit,
                reason="" if used < limit else f"{group.value}_budget_exhausted",
            )

    def record(self, group: ToolBudgetGroup) -> None:
        with self._lock:
            self._counts[group] += 1

    def record_rejection(self, group: ToolBudgetGroup) -> None:
        with self._lock:
            self._rejected[group] += 1

    def summary(self) -> dict:
        with self._lock:
            return {
                group.value: {
                    "used": self._counts[group],
                    "limit": self._limits[group],
                    "remaining": max(0, self._limits[group] - self._counts[group]),
                    "rejected": self._rejected[group],
                }
                for group in ToolBudgetGroup
            }
