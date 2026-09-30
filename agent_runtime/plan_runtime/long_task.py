"""Durable long-task context and evidence continuity primitives."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .models import digest, new_id


@dataclass
class LongTaskState:
    task_id: str
    run_id: str
    original_request: str = ""
    hard_constraints: list[str] = field(default_factory=list)
    key_decisions: list[dict[str, Any]] = field(default_factory=list)
    current_node_id: str = ""
    node_history: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    stale_evidence: list[str] = field(default_factory=list)
    state_revision: int = 0
    schema_version: str = "long-task-v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LongTaskState":
        data = dict(raw or {})
        return cls(
            task_id=str(data.get("task_id", "")),
            run_id=str(data.get("run_id", "")),
            original_request=str(data.get("original_request", "")),
            hard_constraints=[str(v) for v in data.get("hard_constraints", [])],
            key_decisions=[dict(v) for v in data.get("key_decisions", [])],
            current_node_id=str(data.get("current_node_id", "")),
            node_history=[dict(v) for v in data.get("node_history", [])],
            evidence_refs=[str(v) for v in data.get("evidence_refs", [])],
            stale_evidence=[str(v) for v in data.get("stale_evidence", [])],
            state_revision=int(data.get("state_revision", 0) or 0),
            schema_version=str(data.get("schema_version", "long-task-v1")),
        )

    def seal(self) -> dict[str, Any]:
        raw = self.to_dict()
        raw["state_checksum"] = digest(raw)
        return raw

    @classmethod
    def verify(cls, raw: dict[str, Any]) -> "LongTaskState":
        data = dict(raw or {})
        checksum = data.pop("state_checksum", "")
        if not checksum or checksum != digest(data):
            raise ValueError("long_task_state_checksum_invalid")
        state = cls.from_dict(data)
        if not state.task_id or not state.run_id:
            raise ValueError("long_task_state_identity_missing")
        return state


class LongTaskContext:
    """Owns state mutation and builds a node-scoped context projection."""

    def __init__(self, state: LongTaskState, plan, evidence):
        self.state, self.plan, self.evidence = state, plan, evidence

    def _touch(self) -> None:
        self.state.state_revision += 1

    def record_decision(self, decision: str, *, rationale: str = "", source: str = "") -> None:
        self.state.key_decisions.append(
            {"id": new_id("decision"), "decision": decision, "rationale": rationale, "source": source}
        )
        self._touch()

    def set_node(self, node_id: str, status: str = "active") -> None:
        self.state.current_node_id = node_id
        self.state.node_history.append({"node_id": node_id, "status": status})
        self.state.node_history = self.state.node_history[-100:]
        self._touch()

    def add_evidence(self, refs: list[str]) -> None:
        self.state.evidence_refs = list(dict.fromkeys(self.state.evidence_refs + list(refs)))
        self.state.stale_evidence = [r for r in self.state.stale_evidence if r not in refs]
        self._touch()

    def build(self, node_id: str = "") -> dict[str, Any]:
        node_id = node_id or self.state.current_node_id
        node = self.plan.node(node_id) if self.plan and node_id else None
        refs = list(self.state.evidence_refs)
        valid, stale = [], []
        for ref in refs:
            if self.evidence.valid(ref):
                valid.append(ref)
            else:
                stale.append(ref)
        self.state.stale_evidence = list(dict.fromkeys(self.state.stale_evidence + stale))
        return {
            "task": {"id": self.state.task_id, "run_id": self.state.run_id},
            "original_request": self.state.original_request,
            "hard_constraints": list(self.state.hard_constraints),
            "key_decisions": list(self.state.key_decisions),
            "current_node": node.definition() if node else {},
            "node_history": list(self.state.node_history),
            "evidence_refs": valid,
            "stale_evidence": list(self.state.stale_evidence),
            "state_revision": self.state.state_revision,
            "needs_evidence_refresh": bool(stale),
        }

    def render(self, node_id: str = "") -> str:
        return json.dumps(self.build(node_id), ensure_ascii=False, sort_keys=True, indent=2)

    def refresh_evidence(self, ref: str, fetcher: Callable[[], Any]) -> str:
        if ref not in self.state.evidence_refs:
            raise ValueError("unknown_evidence_ref")
        replacement = fetcher()
        if not replacement:
            raise ValueError("evidence_refresh_empty")
        if isinstance(replacement, dict):
            new_ref = str(replacement.get("evidence_id") or replacement.get("observation_id") or "")
        else:
            new_ref = str(replacement)
        if new_ref == ref:
            raise ValueError("evidence_refresh_did_not_replace")
        if not self.evidence.valid(new_ref):
            raise ValueError("evidence_refresh_not_valid")
        self.state.evidence_refs = [new_ref if item == ref else item for item in self.state.evidence_refs]
        self.state.stale_evidence = [item for item in self.state.stale_evidence if item != ref]
        self.state.key_decisions.append({"id": new_id("evidence"), "supersedes": ref, "replacement": new_ref})
        self._touch()
        return new_ref
