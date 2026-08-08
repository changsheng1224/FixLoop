"""Typed task handoff contracts shared by all repair roles."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class HandoffStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"
    EXPIRED = "expired"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass
class AgentTask:
    """A durable unit of work; agents never mutate it directly."""

    task_id: str = field(default_factory=lambda: _new_id("task"))
    run_id: str = ""
    role: str = ""
    kind: str = ""
    phase: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    input_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    priority: int = 0
    deadline_at: float = 0.0
    budget: dict[str, float] = field(default_factory=dict)
    parent_task_id: str = ""
    attempt: int = 0
    max_attempts: int = 1
    status: TaskStatus | str = TaskStatus.PENDING
    version: int = 0
    lease_owner: str = ""
    lease_expires_at: float = 0.0
    retry_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.status = TaskStatus(str(self.status))
        self.priority = int(self.priority or 0)
        self.attempt = max(0, int(self.attempt or 0))
        self.max_attempts = max(1, int(self.max_attempts or 1))
        self.version = max(0, int(self.version or 0))
        self.depends_on = list(dict.fromkeys(str(item) for item in self.depends_on if item))
        self.input_refs = list(dict.fromkeys(str(item) for item in self.input_refs if item))

    @property
    def terminal(self) -> bool:
        return self.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.task_id:
            errors.append("task_id is required")
        if not self.role:
            errors.append("role is required")
        if not self.kind:
            errors.append("kind is required")
        if self.attempt > self.max_attempts:
            errors.append("attempt must be <= max_attempts")
        if self.deadline_at and self.deadline_at < self.created_at:
            errors.append("deadline_at must not precede created_at")
        if self.task_id in self.depends_on:
            errors.append("task cannot depend on itself")
        return errors

    def ready(self, completed_ids: set[str], *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        if self.terminal or self.status not in {TaskStatus.PENDING, TaskStatus.READY}:
            return False
        if self.deadline_at and current >= self.deadline_at:
            return False
        if self.retry_at and current < self.retry_at:
            return False
        return set(self.depends_on).issubset(completed_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "role": self.role,
            "kind": self.kind,
            "phase": self.phase,
            "payload": dict(self.payload),
            "input_refs": list(self.input_refs),
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "deadline_at": self.deadline_at,
            "budget": dict(self.budget),
            "parent_task_id": self.parent_task_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "status": self.status.value,
            "version": self.version,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "retry_at": self.retry_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTask:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class Handoff:
    """Explicit boundary between producer and consumer roles."""

    handoff_id: str = field(default_factory=lambda: _new_id("handoff"))
    task_id: str = ""
    from_role: str = ""
    to_role: str = ""
    input_revision: int = 0
    output_schema: str = ""
    required_evidence: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    deadline_at: float = 0.0
    status: HandoffStatus | str = HandoffStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    accepted_at: float = 0.0

    def __post_init__(self) -> None:
        self.status = HandoffStatus(str(self.status))
        self.required_evidence = list(dict.fromkeys(self.required_evidence or []))

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.handoff_id or not self.task_id:
            errors.append("handoff_id and task_id are required")
        if not self.from_role or not self.to_role:
            errors.append("from_role and to_role are required")
        if self.from_role == self.to_role:
            errors.append("handoff roles must differ")
        if not self.output_schema:
            errors.append("output_schema is required")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "task_id": self.task_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "input_revision": self.input_revision,
            "output_schema": self.output_schema,
            "required_evidence": list(self.required_evidence),
            "payload": dict(self.payload),
            "deadline_at": self.deadline_at,
            "status": self.status.value,
            "created_at": self.created_at,
            "accepted_at": self.accepted_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Handoff:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class AgentResult:
    task_id: str
    status: TaskStatus | str = TaskStatus.COMPLETED
    output: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    side_effect_ids: list[str] = field(default_factory=list)
    error: str = ""
    attempt: int = 0
    produced_at: float = field(default_factory=time.time)
    consumed_by: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = TaskStatus(str(self.status))
        self.evidence_refs = list(dict.fromkeys(self.evidence_refs or []))
        self.side_effect_ids = list(dict.fromkeys(self.side_effect_ids or []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "output": dict(self.output),
            "evidence_refs": list(self.evidence_refs),
            "side_effect_ids": list(self.side_effect_ids),
            "error": self.error,
            "attempt": self.attempt,
            "produced_at": self.produced_at,
            "consumed_by": list(self.consumed_by),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentResult:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})
