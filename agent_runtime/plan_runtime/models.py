"""Immutable, versioned small task plans. No Layer 2 dependencies."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def new_id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


@dataclass(frozen=True)
class Completion:
    kind: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    kind: str
    objective: str
    depends_on: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
    side_effect: str = "read"
    completion: tuple[Completion, ...] = ()
    input_evidence_refs: tuple[str, ...] = ()
    output_evidence_refs: tuple[str, ...] = ()
    status: str = "pending"
    attempt_id: str = ""
    failure: str = ""
    receipt_refs: tuple[str, ...] = ()
    started_at: float = 0
    ended_at: float = 0
    # Fixed operations only; JSON text keeps frozen snapshots deeply immutable.
    tool_name: str = ""
    arguments_json: str = "{}"

    def definition(self) -> dict:
        raw = asdict(self)
        for key in (
            "output_evidence_refs",
            "status",
            "attempt_id",
            "failure",
            "receipt_refs",
            "started_at",
            "ended_at",
        ):
            raw.pop(key)
        return raw


@dataclass(frozen=True)
class Plan:
    plan_id: str
    task_id: str
    run_id: str
    workspace_id: str
    session_id: str
    nodes: tuple[PlanNode, ...]
    schema_version: str = "1"
    plan_version: int = 1
    state_revision: int = 0
    created_at: float = field(default_factory=time.time)
    status: str = "active"
    plan_checksum: str = ""
    parent_plan_checksum: str = ""
    replan_reason: str = ""
    replan_evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    def seal(self) -> Plan:
        raw = self.to_dict()
        raw.pop("plan_checksum")
        return replace(self, plan_checksum=digest(raw))

    def verify(self) -> bool:
        return bool(self.plan_checksum) and self.seal().plan_checksum == self.plan_checksum

    def node(self, node_id: str) -> PlanNode:
        return next(node for node in self.nodes if node.node_id == node_id)

    @classmethod
    def from_dict(cls, raw: dict) -> Plan:
        data = dict(raw)
        nodes = []
        for item in data.pop("nodes"):
            item = dict(item)
            item["completion"] = tuple(
                Completion(c["kind"], tuple(c.get("evidence_refs", ())))
                for c in item.get("completion", ())
            )
            for key in (
                "depends_on",
                "tool_allowlist",
                "input_evidence_refs",
                "output_evidence_refs",
                "receipt_refs",
            ):
                item[key] = tuple(item.get(key, ()))
            nodes.append(PlanNode(**item))
        data["replan_evidence_refs"] = tuple(data.get("replan_evidence_refs", ()))
        return cls(nodes=tuple(nodes), **data)


@dataclass(frozen=True)
class NodeAttempt:
    attempt_id: str
    plan_id: str
    plan_version: int
    node_id: str
    state_revision_at_dispatch: int
    workspace_before: dict
    allowed_tools: tuple[str, ...]
    idempotency_key: str
    phase: str = "prepared"
    kind: str = "explore"
    owner: dict = field(default_factory=dict)
    workspace_after: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    terminal_status: str = ""
