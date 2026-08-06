"""Governed context runtime primitives.

The runtime owns selection, provenance and resume safety. It does not make
repair decisions for the model.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ContextRequest:
    phase: str = "repair"
    intent: str = ""
    active_hypothesis_ids: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    failure_bucket: str = ""
    next_action: str = ""
    token_budget: int = 2000


@dataclass
class ContextItem:
    item_id: str
    kind: str
    content: str
    source_ref: str = ""
    token_cost: int = 0
    relevance: float = 0.0
    confidence: float = 0.5
    freshness: float = 1.0
    evidence_strength: float = 0.0
    hypothesis_ids: list[str] = field(default_factory=list)
    scope: str = "task"
    stale: bool = False

    def utility(self, request: ContextRequest) -> float:
        phase_factor = {
            "explore": 1.0,
            "patch": 1.15,
            "verify": 1.1,
            "repair": 1.0,
        }.get(request.phase, 1.0)
        alignment = 1.0 if set(self.hypothesis_ids) & set(request.active_hypothesis_ids) else 0.6
        scope_bonus = 1.0 if self.scope in {"task", "run"} else 0.9
        raw = (
            0.45 * self.relevance
            + 0.2 * self.confidence
            + 0.15 * self.freshness
            + 0.2 * self.evidence_strength
        )
        return raw * phase_factor * alignment * scope_bonus / max(self.token_cost, 1)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContextPolicyEngine:
    """Select highest-value context items under a token budget."""

    def select(self, items: list[ContextItem], request: ContextRequest) -> list[ContextItem]:
        available = [item for item in items if not item.stale]
        ranked = sorted(available, key=lambda item: item.utility(request), reverse=True)
        selected: list[ContextItem] = []
        used = 0
        for item in ranked:
            if used + max(item.token_cost, 0) > request.token_budget:
                continue
            selected.append(item)
            used += max(item.token_cost, 0)
        return selected


@dataclass
class Observation:
    observation_id: str
    tool: str
    args_hash: str
    source_version: str
    summary: str
    raw_ref: str
    structured_facts: list[dict[str, Any]] = field(default_factory=list)
    token_cost: int = 0
    created_at: float = field(default_factory=time.time)
    stale: bool = False


class ObservationStore:
    """Deduplicated, provenance-preserving tool observations."""

    def __init__(self, state: dict[str, Any], root: str = ""):
        self.state = state
        self.root = Path(root) if root else None
        self.registry = state.setdefault("observations", {})

    @staticmethod
    def _args_hash(args: dict[str, Any]) -> str:
        raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def put(
        self,
        tool: str,
        args: dict[str, Any],
        raw_text: str,
        *,
        summary: str = "",
        source_version: str = "",
        structured_facts: list[dict[str, Any]] | None = None,
    ) -> Observation:
        args_hash = self._args_hash(args)
        key = f"{tool}:{args_hash}:{source_version}"
        existing_id = self.state.setdefault("observation_index", {}).get(key)
        if existing_id and existing_id in self.registry:
            existing = Observation(**self.registry[existing_id])
            if not existing.stale:
                return existing
        observation_id = "OBS-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        raw_ref = self._persist_raw(observation_id, raw_text)
        observation = Observation(
            observation_id=observation_id,
            tool=tool,
            args_hash=args_hash,
            source_version=source_version,
            summary=summary or raw_text[:500],
            raw_ref=raw_ref,
            structured_facts=list(structured_facts or []),
            token_cost=max(1, len((summary or raw_text).split())),
        )
        self.registry[observation_id] = asdict(observation)
        self.state.setdefault("observation_index", {})[key] = observation_id
        return observation

    def mark_stale_for_version(self, source_version: str) -> int:
        count = 0
        for raw in self.registry.values():
            if source_version and raw.get("source_version") == source_version:
                raw["stale"] = True
                count += 1
        return count

    def expand(self, observation_id: str) -> str:
        raw = self.registry.get(observation_id)
        if not raw or not raw.get("raw_ref"):
            return ""
        path = Path(raw["raw_ref"])
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            return ""

    def _persist_raw(self, observation_id: str, raw_text: str) -> str:
        if self.root is None:
            return "memory:" + observation_id
        path = self.root / ".agent" / "observations" / f"{observation_id}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(raw_text, encoding="utf-8")
            return str(path)
        except OSError:
            return "memory:" + observation_id


@dataclass
class Hypothesis:
    hypothesis_id: str
    statement: str
    target_files: list[str] = field(default_factory=list)
    status: str = "active"
    confidence: float = 0.5
    evidence_ids: list[str] = field(default_factory=list)
    counter_evidence_ids: list[str] = field(default_factory=list)


class HypothesisEvidenceGraph:
    def __init__(self, state: dict[str, Any]):
        self.state = state
        self.hypotheses = state.setdefault("hypotheses", {})
        self.evidence = state.setdefault("evidence", {})

    def add_hypothesis(
        self, statement: str, *, target_files: list[str] | None = None
    ) -> Hypothesis:
        hid = "H-" + hashlib.sha256(statement.encode()).hexdigest()[:12]
        item = Hypothesis(hid, statement, list(target_files or []))
        self.hypotheses[hid] = asdict(item)
        return item

    def link(self, hypothesis_id: str, evidence_id: str, relation: str) -> bool:
        raw = self.hypotheses.get(hypothesis_id)
        if not raw or relation not in {"supports", "contradicts", "neutral"}:
            return False
        target = "evidence_ids" if relation == "supports" else "counter_evidence_ids"
        if evidence_id not in raw[target]:
            raw[target].append(evidence_id)
        if relation == "supports":
            raw["confidence"] = min(1.0, float(raw["confidence"]) + 0.1)
        elif relation == "contradicts":
            raw["confidence"] = max(0.0, float(raw["confidence"]) - 0.2)
            if raw["confidence"] < 0.2:
                raw["status"] = "rejected"
        return True

    def update_from_verification(self, passed: bool) -> None:
        for raw in self.hypotheses.values():
            if raw.get("status") == "rejected":
                continue
            if passed:
                raw["status"] = "supported"
                raw["confidence"] = min(1.0, float(raw.get("confidence", 0.5)) + 0.15)
            elif raw.get("counter_evidence_ids"):
                raw["status"] = "rejected"


@dataclass
class ActionRecord:
    action_id: str
    tool: str
    args_hash: str
    precondition_revision: int
    result_ref: str = ""
    side_effect: str = "none"
    replay_policy: str = "revalidate"


def build_context_manifest(
    state: dict[str, Any], *, workspace_fingerprint: str = ""
) -> dict[str, Any]:
    return {
        "state_revision": int(state.get("state_revision", 0) or 0),
        "workspace_fingerprint": workspace_fingerprint,
        "active_hypothesis_ids": list(state.get("active_hypothesis_ids", [])),
        "selected_context_ids": list(state.get("selected_context_ids", [])),
        "observation_refs": list(state.get("recalled_observation_ids", [])),
        "memory_refs": list(state.get("recalled_memory_ids", [])),
        "compressed_history_ref": str(state.get("compressed_history_ref", "") or ""),
        "file_versions": dict(state.get("file_versions", {}) or {}),
    }


def build_action_record(
    tool: str,
    args: dict[str, Any],
    *,
    revision: int,
    result_ref: str = "",
    side_effect: str = "none",
) -> ActionRecord:
    raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=True, default=str)
    return ActionRecord(
        action_id="ACT-" + hashlib.sha256(f"{tool}:{raw}".encode()).hexdigest()[:12],
        tool=tool,
        args_hash=hashlib.sha256(raw.encode()).hexdigest()[:16],
        precondition_revision=revision,
        result_ref=result_ref,
        side_effect=side_effect,
        replay_policy="never_replay" if side_effect in {"write", "external"} else "revalidate",
    )


def append_action(state: dict[str, Any], action: ActionRecord) -> dict[str, Any]:
    """Persist an action record and return its serialized form."""
    raw = asdict(action)
    state.setdefault("action_ledger", []).append(raw)
    state["action_ledger"] = state["action_ledger"][-100:]
    return raw


def replay_policy(state: dict[str, Any], tool: str, args: dict[str, Any]) -> str:
    """Return reuse/revalidate/never_replay for a matching prior action."""
    raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=True, default=str)
    args_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    matches = [
        item for item in state.get("action_ledger", [])
        if item.get("tool") == tool and item.get("args_hash") == args_hash
    ]
    if not matches:
        return "revalidate"
    return str(matches[-1].get("replay_policy", "revalidate"))
