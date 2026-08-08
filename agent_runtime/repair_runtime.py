"""Provider-neutral repair runtime contracts and run-wide controls.

The model chooses a tool and repair strategy.  This module only normalizes,
authorizes, budgets, and observes execution.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolSource(StrEnum):
    NATIVE = "native"
    TEXT = "text"
    RECOVERED = "recovered"


class ToolRejectCode(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    ROLE_NOT_ALLOWED = "role_not_allowed"
    MODE_NOT_ALLOWED = "mode_not_allowed"
    PHASE_NOT_ALLOWED = "phase_not_allowed"
    FILE_NOT_READ = "file_not_read"
    EDIT_LOCKED = "edit_locked"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DUPLICATE_CALL = "duplicate_call"
    OUTPUT_TOO_LARGE = "output_too_large"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_CANCELLED = "tool_cancelled"
    EXECUTION_FAILED = "tool_execution_failed"
    UNCERTAIN = "uncertain"
    RETRY_EXHAUSTED = "retry_exhausted"
    CIRCUIT_OPEN = "circuit_open"
    STALE_PRECONDITION = "stale_precondition"


@dataclass(frozen=True)
class CanonicalToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    source: ToolSource = ToolSource.NATIVE
    raw_name: str = ""
    raw_arguments: str = ""
    parse_warnings: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        source=ToolSource.NATIVE,
        call_id="",
    ):
        args = arguments if isinstance(arguments, dict) else {}
        stable = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        identifier = call_id or "call_" + hashlib.sha256(
            f"{name}:{stable}".encode()
        ).hexdigest()[:12]
        return cls(identifier, str(name), args, ToolSource(source), str(name), stable)


@dataclass
class ToolObservation:
    call_id: str = ""
    tool_name: str = ""
    status: str = "success"
    content: str = ""
    exit_code: int | None = None
    changed_files: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    retryable: bool = False
    failure_class: str = ""
    duration_ms: int = 0
    output_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ExecutionDeadline:
    def __init__(self, timeout_s: float = 0):
        self.started_at = time.monotonic()
        self.deadline_at = self.started_at + float(timeout_s) if timeout_s > 0 else None

    @classmethod
    def from_remaining(cls, remaining_s: float | None) -> ExecutionDeadline:
        deadline = cls(0)
        if remaining_s is not None:
            deadline.deadline_at = time.monotonic() + max(0.0, float(remaining_s))
        return deadline

    def snapshot(self) -> dict[str, float | None]:
        return {
            "remaining_s": self.remaining_s(),
            "started_at_monotonic": self.started_at,
        }

    def remaining_s(self) -> float | None:
        if self.deadline_at is None:
            return None
        return max(0.0, self.deadline_at - time.monotonic())

    def expired(self) -> bool:
        remaining = self.remaining_s()
        return remaining is not None and remaining <= 0

    def check(self) -> None:
        if self.expired():
            raise TimeoutError("repair deadline exceeded")


@dataclass
class RepairBudget:
    max_turns: int = 0
    max_tool_calls: int = 0
    max_write_calls: int = 0
    max_verify_calls: int = 0
    max_recovery_attempts: int = 0
    turns: int = 0
    tool_calls: int = 0
    writes: int = 0
    verifies: int = 0
    recoveries: int = 0

    def allow_turn(self) -> bool:
        return self.max_turns <= 0 or self.turns < self.max_turns

    def allow_tool(self, group: str) -> bool:
        if self.max_tool_calls > 0 and self.tool_calls >= self.max_tool_calls:
            return False
        limits = {
            "write": (self.writes, self.max_write_calls),
            "verify": (self.verifies, self.max_verify_calls),
            "recovery": (self.recoveries, self.max_recovery_attempts),
        }
        used, limit = limits.get(group, (0, 0))
        return limit <= 0 or used < limit

    def record_turn(self) -> None:
        self.turns += 1

    def record_tool(self, group: str) -> None:
        self.tool_calls += 1
        if group == "write":
            self.writes += 1
        elif group == "verify":
            self.verifies += 1
        elif group == "recovery":
            self.recoveries += 1

    def summary(self) -> dict[str, int]:
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "writes": self.writes,
            "verifies": self.verifies,
            "recoveries": self.recoveries,
        }

    def snapshot(self) -> dict[str, int]:
        return {
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_write_calls": self.max_write_calls,
            "max_verify_calls": self.max_verify_calls,
            "max_recovery_attempts": self.max_recovery_attempts,
            **self.summary(),
        }

    def restore(self, snapshot: dict[str, int] | None) -> None:
        data = snapshot or {}
        for key in (
            "max_turns",
            "max_tool_calls",
            "max_write_calls",
            "max_verify_calls",
            "max_recovery_attempts",
            "turns",
            "tool_calls",
            "writes",
            "verifies",
            "recoveries",
        ):
            if key in data:
                setattr(self, key, max(0, int(data[key] or 0)))


def observation_from_result(
    call: CanonicalToolCall, result, duration_ms: int = 0
) -> ToolObservation:
    metadata = getattr(result, "metadata", None) or {}
    status = str(metadata.get("tool_status", "success"))
    if status == "rejected":
        failure = str(metadata.get("tool_error_code", "tool_rejected"))
        denied = {"permission_denied", "approval_denied", "role_not_allowed"}
        status = "permission_denied" if failure in denied else "validation_error"
    elif status == "error":
        status = (
            "timeout"
            if metadata.get("tool_error_code") == "tool_timeout"
            else "execution_error"
        )
    elif status == "uncertain":
        status = "uncertain"
    elif status == "cancelled":
        status = "cancelled"
    return ToolObservation(
        call_id=call.call_id,
        tool_name=call.name,
        status=status,
        content=str(getattr(result, "content", result)),
        changed_files=list(metadata.get("affected_paths") or []),
        retryable=bool(
            metadata.get("retryable", status in {"validation_error", "execution_error"})
        ),
        failure_class=str(metadata.get("tool_error_code", "") or ""),
        duration_ms=duration_ms,
    )


def tool_observation_summary(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Build compact report metrics without retaining full tool output."""
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for item in observations:
        status = str(item.get("status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        source = str(item.get("source", "native"))
        by_source[source] = by_source.get(source, 0) + 1
    return {"count": len(observations), "by_status": by_status, "by_source": by_source}
