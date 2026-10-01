"""Immutable coordination values passed across execution boundaries."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class OwnerLease:
    task_id: str
    run_id: str
    workspace_id: str
    owner_token: str
    generation: int
    coordination_revision: int
    lease_expires_at: float
    status: str = "reconciling"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ResourceRecord:
    resource_id: str
    task_id: str
    run_id: str
    workspace_id: str
    parent_id: str = ""
    kind: str = "plan_attempt"
    effect: str = "read"
    status: str = "planned"
    generation: int = 0
    owner_token: str = ""
    receipt_ref: str = ""
    cleanup: str = "unverified"
    error_code: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ResourceResult:
    resource_id: str
    status: str
    cleanup: str = "unknown"
    error_code: str = ""
    receipt_ref: str = ""
    changed_paths: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.cleanup == "confirmed" and self.status in {
            "cancelled",
            "completed",
            "failed",
            "not_started",
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["changed_paths"] = list(self.changed_paths)
        return value


@dataclass(frozen=True)
class CancelReport:
    run_id: str
    request_id: str
    status: str
    resources: tuple[ResourceResult, ...] = ()
    error_code: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def confirmed(self) -> bool:
        return self.status == "cancelled" and all(item.confirmed for item in self.resources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "status": self.status,
            "resources": [item.to_dict() for item in self.resources],
            "error_code": self.error_code,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RunSnapshot:
    task_id: str
    run_id: str
    workspace_id: str
    workspace: str
    status: str
    generation: int
    owner_token: str
    coordination_revision: int
    lease_expires_at: float
    cancel_request_id: str = ""
    resources: tuple[ResourceRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "resources": [item.to_dict() for item in self.resources],
        }


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def process_owner_identity() -> dict[str, Any]:
    """Capture process creation identity; PID reuse cannot prove a live owner."""
    from agent_runtime.plan_runtime.processes import process_identity

    return process_identity(os.getpid()) or {"pid": os.getpid(), "generation": "unknown"}
