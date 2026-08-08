"""Memory Governance Service.

This service governs memory lifecycle; it never decides the current repair.
Candidates are evidence-bound and scoped before they can become durable facts.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def _query_tokens(text: str) -> set[str]:
    import re

    return {token for token in re.findall(r"[a-zA-Z0-9_./:-]+", str(text).lower()) if token}


class MemoryScope(StrEnum):
    RUN = "run"
    TASK = "task"
    REPOSITORY = "repository"
    REPOSITORY_VERSION = "repository_version"
    USER = "user"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    VERIFIED = "verified"
    ACTIVE = "active"
    DURABLE = "durable"
    STALE = "stale"
    CONFLICTED = "conflicted"
    DEMOTED = "demoted"
    REJECTED = "rejected"


class MemoryAuthority(StrEnum):
    MODEL_INFERENCE = "model_inference"
    EXTERNAL_CONTEXT = "external_context"
    HISTORICAL_SUCCESS = "historical_success"
    VERIFIED_TOOL = "verified_tool"
    USER_CONFIRMED = "user_confirmed"
    REPO_POLICY = "repo_policy"
    CURRENT_USER = "current_user"


class ConflictType(StrEnum):
    HARD_CONTRADICTION = "hard_contradiction"
    TEMPORAL_UPDATE = "temporal_update"
    SCOPE_SHADOW = "scope_shadow"
    MULTI_VALUE = "multi_value"
    SOURCE_DISAGREEMENT = "source_disagreement"


class ConflictStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


_AUTHORITY_RANK = {
    MemoryAuthority.MODEL_INFERENCE.value: 10,
    MemoryAuthority.EXTERNAL_CONTEXT.value: 20,
    MemoryAuthority.HISTORICAL_SUCCESS.value: 30,
    MemoryAuthority.VERIFIED_TOOL.value: 40,
    MemoryAuthority.USER_CONFIRMED.value: 50,
    MemoryAuthority.REPO_POLICY.value: 60,
    MemoryAuthority.CURRENT_USER.value: 70,
}

_EXTERNAL_SOURCE_TYPES = frozenset({"mcp", "web", "external"})
_LOCAL_EVIDENCE_PREFIXES = ("repo:", "tool:", "verify:", "file:", "test:")


@dataclass
class PolicyRecord:
    policy_id: str
    key: str
    value: str
    authority: str = MemoryAuthority.REPO_POLICY.value
    scope: str = MemoryScope.REPOSITORY.value
    source: str = ""
    path_prefix: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ConflictRecord:
    conflict_id: str
    key: str
    scope: str
    candidate_ids: list[str]
    conflict_type: str
    status: str = ConflictStatus.PENDING.value
    winner_ids: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    resolver: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class GovernedMemory:
    memory_id: str
    key: str
    value: str
    kind: str = "observation"
    scope: str = MemoryScope.TASK.value
    source: str = ""
    source_type: str = "model"
    confidence: float = 0.4
    evidence_refs: list[str] = field(default_factory=list)
    repo_fingerprint: str = ""
    status: str = MemoryStatus.CANDIDATE.value
    verification_count: int = 0
    retrieve_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    last_verified_at: float = 0.0
    conflict_status: str = "none"
    conflict_ids: list[str] = field(default_factory=list)
    user_id: str = ""
    task_id: str = ""
    usage_successes: int = 0
    usage_failures: int = 0
    authority: str = MemoryAuthority.MODEL_INFERENCE.value
    trust_domain: str = "local"
    tainted: bool = False
    cross_confirmed: bool = False
    allow_multiple: bool = False
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class MemoryUsageEvent:
    """Traceable evidence of how a recalled memory affected a run."""

    memory_id: str
    task_id: str = ""
    trace_id: str = ""
    stage: str = "context"
    usage: str = "recalled"
    outcome: str = "inconclusive"
    evidence_refs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def repository_fingerprint(root: str) -> str:
    """Return a cheap identity fingerprint; no model or network involved."""
    try:
        from pathlib import Path

        path = Path(root)
        head = ""
        git_path = path / ".git"
        git_head = git_path / "HEAD" if git_path.is_dir() else git_path
        if git_head.is_file():
            head = git_head.read_text(encoding="utf-8", errors="ignore")[:200]
        manifests = []
        for name in ("pyproject.toml", "package.json", "pom.xml", "go.mod", "Cargo.toml"):
            file = path / name
            if file.is_file():
                manifests.append(f"{name}:{file.stat().st_mtime_ns}:{file.stat().st_size}")
        raw = f"{path.resolve()}|{head}|{'|'.join(manifests)}"
    except OSError:
        raw = str(root)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def normalize_candidate(
    candidate, *, scope: str = "", repo_fingerprint: str = ""
) -> GovernedMemory:
    """Convert legacy Candidate to a governed candidate with safe defaults."""
    source = str(getattr(candidate, "source", "") or "")
    explicit_source_type = str(getattr(candidate, "source_type", "") or "").lower()
    source_type = explicit_source_type or (
        "user" if source == "user" else "tool" if source else "model"
    )
    kind = str(getattr(candidate, "kind", "observation") or "observation")
    selected_scope = scope or (
        MemoryScope.USER.value if source_type == "user" else MemoryScope.TASK.value
    )
    if source_type == "user" and selected_scope == MemoryScope.TASK.value:
        selected_scope = MemoryScope.USER.value
    raw_key = str(getattr(candidate, "key", "") or "value")
    memory_id = "M-" + hashlib.sha256(
        f"{selected_scope}|{raw_key}|{getattr(candidate, 'value', '')}".encode()
    ).hexdigest()[:12]
    confidence = float(getattr(candidate, "confidence", 0.4) or 0.4)
    if source_type == "model":
        confidence = min(confidence, 0.49)
    tainted = source_type in _EXTERNAL_SOURCE_TYPES
    authority = str(getattr(candidate, "authority", "") or "")
    if not authority:
        authority = (
            MemoryAuthority.CURRENT_USER.value
            if source_type == "user"
            else MemoryAuthority.EXTERNAL_CONTEXT.value
            if tainted
            else MemoryAuthority.VERIFIED_TOOL.value
            if source_type in {"tool", "repo"}
            else MemoryAuthority.MODEL_INFERENCE.value
        )
    return GovernedMemory(
        memory_id=memory_id,
        key=raw_key,
        value=str(getattr(candidate, "value", "") or ""),
        kind=kind,
        scope=selected_scope,
        source=source,
        source_type=source_type,
        confidence=max(0.0, min(1.0, confidence)),
        repo_fingerprint=repo_fingerprint,
        authority=authority,
        trust_domain="external" if tainted else "local",
        tainted=tainted,
        allow_multiple=bool(getattr(candidate, "allow_multiple", False)),
    )


def can_promote(memory: GovernedMemory, *, user_confirmed: bool = False) -> tuple[bool, str]:
    if memory.status in {
        MemoryStatus.STALE.value,
        MemoryStatus.CONFLICTED.value,
        MemoryStatus.DEMOTED.value,
        MemoryStatus.REJECTED.value,
    }:
        return False, f"invalid_status:{memory.status}"
    if memory.scope in {MemoryScope.RUN.value, MemoryScope.TASK.value}:
        return False, "task_or_run_scope"
    if memory.source_type == "model" and not user_confirmed:
        return False, "model_inference_requires_confirmation"
    if memory.tainted and not (memory.cross_confirmed or user_confirmed):
        return False, "external_context_requires_local_confirmation"
    if not memory.evidence_refs and not user_confirmed:
        return False, "missing_evidence"
    if memory.conflict_status != "none":
        return False, "conflicted"
    if memory.verification_count < 1 and not user_confirmed:
        return False, "not_verified"
    if memory.confidence < 0.8 and not user_confirmed:
        return False, "confidence_below_threshold"
    return True, "eligible"


class MemoryGovernanceService:
    """In-memory governance layer used by Dream and candidate promotion."""

    def __init__(
        self,
        state: dict,
        *,
        repo_root: str = "",
        user_id: str = "",
        task_id: str = "",
        probe: Callable[[ConflictRecord, list[dict[str, Any]]], dict[str, Any] | None]
        | None = None,
    ):
        self.state = state
        self.repo_root = repo_root
        self.user_id = str(user_id or "")
        self.task_id = str(task_id or "")
        self.probe = probe
        self.current_repo_fingerprint = repository_fingerprint(repo_root) if repo_root else ""
        self.audit_log = state.setdefault("memory_governance_audit", [])
        self.registry = state.setdefault("governed_memories", {})
        self.policies = state.setdefault("memory_policies", {})
        self.conflicts = state.setdefault("memory_conflicts", {})
        self.store = None
        if repo_root:
            try:
                from agent_runtime.features.memory.store import CanonicalMemoryStore

                self.store = CanonicalMemoryStore(repo_root)
                self._hydrate_from_store()
            except (OSError, sqlite3.Error):
                self.store = None
        self.stats = {
            "normalized": 0,
            "supported": 0,
            "verified": 0,
            "promoted": 0,
            "demoted": 0,
            "stale_marked": 0,
            "conflicts_detected": 0,
            "conflicts_resolved": 0,
            "conflicts_unresolved": 0,
            "policy_shadowed": 0,
            "probe_attempts": 0,
            "rejected": 0,
            "recall_hits": 0,
            "usage_events": 0,
            "usage_supported": 0,
            "usage_contradicted": 0,
            "usage_inconclusive": 0,
            "revalidation_queued": 0,
        }

    def _hydrate_from_store(self) -> None:
        if not self.store or self.registry:
            return
        for memory in self.store.list_memories():
            memory_id = str(memory.get("memory_id", ""))
            if memory_id:
                self.registry[memory_id] = memory

    def _persist_memory(self, memory_id: str) -> None:
        if self.store and memory_id in self.registry:
            self.store.upsert_memory(dict(self.registry[memory_id]))

    def _persist_audit(self, action: str, object_id: str, payload: dict[str, Any]) -> None:
        if self.store:
            self.store.append_audit(action, object_id, payload)

    def record_recall(
        self,
        memory_ids: list[str],
        *,
        task_id: str = "",
        trace_id: str = "",
        stage: str = "context",
    ) -> list[dict[str, Any]]:
        """Record recall without treating retrieval as verification."""
        events = self.state.setdefault("memory_usage_events", [])
        created: list[dict[str, Any]] = []
        normalized_ids = list(dict.fromkeys(str(item) for item in memory_ids if item))
        self.state["recalled_memory_ids"] = normalized_ids
        for memory_id in normalized_ids:
            event = MemoryUsageEvent(
                memory_id=memory_id,
                task_id=str(task_id or self.task_id),
                trace_id=str(trace_id or ""),
                stage=str(stage or "context"),
                usage="recalled",
                outcome="inconclusive",
            ).to_dict()
            events.append(event)
            created.append(event)
            if self.store:
                self.store.append_usage_event(event)
        del events[:-200]
        return created

    def record_usage_stage(
        self,
        memory_id: str,
        *,
        usage: str,
        stage: str,
        task_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        """Record projected/cited/applied without changing trust scores."""
        event = MemoryUsageEvent(
            memory_id=str(memory_id),
            task_id=str(task_id or self.task_id),
            trace_id=str(trace_id or ""),
            stage=str(stage or "context"),
            usage=str(usage or "projected"),
            outcome="inconclusive",
        ).to_dict()
        events = self.state.setdefault("memory_usage_events", [])
        events.append(event)
        del events[:-200]
        self.stats["usage_events"] += 1
        if self.store:
            self.store.append_usage_event(event)
        return event

    def _audit(self, action: str, memory: GovernedMemory, reason: str = "") -> None:
        self.audit_log.append(
            {
                "ts": time.time(),
                "action": action,
                "memory_id": memory.memory_id,
                "scope": memory.scope,
                "status": memory.status,
                "reason": reason,
            }
        )
        del self.audit_log[:-200]
        self._persist_audit(action, memory.memory_id, memory.to_dict())

    def _audit_control(
        self, action: str, object_id: str, *, reason: str = "", metadata: dict | None = None
    ) -> None:
        self.audit_log.append(
            {
                "ts": time.time(),
                "action": action,
                "object_id": object_id,
                "reason": reason,
                "metadata": dict(metadata or {}),
            }
        )
        del self.audit_log[:-200]
        self._persist_audit(
            action,
            object_id,
            {"reason": reason, "metadata": dict(metadata or {})},
        )

    def add_policy(
        self,
        key: str,
        value: str,
        *,
        authority: str = MemoryAuthority.REPO_POLICY.value,
        scope: str = MemoryScope.REPOSITORY.value,
        source: str = "",
        path_prefix: str = "",
    ) -> PolicyRecord:
        """Register explicit guidance separately from generated memory."""
        normalized_key = str(key).strip().lower()
        policy_id = "P-" + hashlib.sha256(
            f"{authority}|{scope}|{path_prefix}|{normalized_key}|{value}".encode()
        ).hexdigest()[:12]
        policy = PolicyRecord(
            policy_id=policy_id,
            key=str(key).strip(),
            value=str(value),
            authority=str(authority),
            scope=str(scope),
            source=str(source),
            path_prefix=str(path_prefix),
        )
        self.policies[policy_id] = policy.to_dict()
        self._audit_control("policy_add", policy_id, metadata={"key": normalized_key})
        return policy

    def effective_policy(self, key: str, *, path: str = "") -> dict[str, Any] | None:
        """Return the highest-authority, most-specific matching policy."""
        normalized_key = str(key).strip().lower()
        matches = []
        for raw in self.policies.values():
            if str(raw.get("key", "")).strip().lower() != normalized_key:
                continue
            prefix = str(raw.get("path_prefix", "") or "")
            if prefix and not str(path).replace("\\", "/").startswith(prefix.replace("\\", "/")):
                continue
            matches.append(raw)
        if not matches:
            return None
        return max(
            matches,
            key=lambda raw: (
                _AUTHORITY_RANK.get(str(raw.get("authority", "")), 0),
                len(str(raw.get("path_prefix", ""))),
                float(raw.get("created_at", 0.0)),
            ),
        )

    def ingest(self, candidate, *, scope: str = "", repo_fingerprint: str = "") -> GovernedMemory:
        memory = normalize_candidate(
            candidate,
            scope=scope,
            repo_fingerprint=repo_fingerprint or self.current_repo_fingerprint,
        )
        memory.user_id = self.user_id if memory.scope == MemoryScope.USER.value else ""
        memory.task_id = self.task_id if memory.scope in {
            MemoryScope.RUN.value,
            MemoryScope.TASK.value,
        } else ""
        self.registry[memory.memory_id] = memory.to_dict()
        self._persist_memory(memory.memory_id)
        self.stats["normalized"] += 1
        self._audit("ingest", memory)
        return memory

    def bind_evidence(self, memory_id: str, refs: list[str]) -> bool:
        raw = self.registry.get(memory_id)
        if not raw:
            return False
        raw["evidence_refs"] = list(dict.fromkeys([*raw.get("evidence_refs", []), *refs]))
        if raw.get("tainted") and any(
            str(ref).lower().startswith(_LOCAL_EVIDENCE_PREFIXES) for ref in refs
        ):
            raw["cross_confirmed"] = True
        if raw["evidence_refs"] and raw["status"] == MemoryStatus.CANDIDATE.value:
            raw["status"] = MemoryStatus.SUPPORTED.value
            self.stats["supported"] += 1
        self._audit("bind_evidence", GovernedMemory(**raw))
        return True

    def validate(
        self, memory_id: str, *, passed: bool, evidence_refs: list[str] | None = None
    ) -> bool:
        raw = self.registry.get(memory_id)
        if not raw:
            return False
        memory = GovernedMemory(**raw)
        if evidence_refs:
            memory.evidence_refs = list(dict.fromkeys([*memory.evidence_refs, *evidence_refs]))
        if passed:
            memory.verification_count += 1
            memory.confidence = min(1.0, memory.confidence + 0.15)
            memory.last_verified_at = time.time()
            memory.status = MemoryStatus.VERIFIED.value
            self.stats["verified"] += 1
            self._audit("validate_pass", memory)
        else:
            memory.confidence = max(0.0, memory.confidence - 0.2)
            memory.status = MemoryStatus.DEMOTED.value
            self.stats["demoted"] += 1
            self._audit("validate_fail", memory)
        self.registry[memory.memory_id] = memory.to_dict()
        self._persist_memory(memory.memory_id)
        return passed

    @staticmethod
    def _conflict_id(scope: str, key: str, candidate_ids: list[str]) -> str:
        joined = "|".join(sorted(candidate_ids))
        return "C-" + hashlib.sha256(f"{scope}|{key}|{joined}".encode()).hexdigest()[:12]

    def _classify_conflict(
        self, left: GovernedMemory, right: GovernedMemory
    ) -> ConflictType:
        if left.allow_multiple or right.allow_multiple:
            return ConflictType.MULTI_VALUE
        if (
            left.scope == MemoryScope.REPOSITORY_VERSION.value
            and right.scope == MemoryScope.REPOSITORY_VERSION.value
            and left.repo_fingerprint
            and right.repo_fingerprint
            and left.repo_fingerprint != right.repo_fingerprint
        ):
            return ConflictType.TEMPORAL_UPDATE
        if left.authority != right.authority or left.source_type != right.source_type:
            return ConflictType.SOURCE_DISAGREEMENT
        return ConflictType.HARD_CONTRADICTION

    def _create_conflict(
        self, left: GovernedMemory, right: GovernedMemory, conflict_type: ConflictType
    ) -> ConflictRecord:
        candidate_ids = list(dict.fromkeys([left.memory_id, right.memory_id]))
        conflict_id = self._conflict_id(left.scope, left.key.lower().strip(), candidate_ids)
        existing = self.conflicts.get(conflict_id)
        if existing:
            return ConflictRecord(**existing)
        record = ConflictRecord(
            conflict_id=conflict_id,
            key=left.key,
            scope=left.scope,
            candidate_ids=candidate_ids,
            conflict_type=conflict_type.value,
        )
        self.conflicts[conflict_id] = record.to_dict()
        self.stats["conflicts_detected"] += 1
        self._audit_control(
            "conflict_detected", conflict_id, metadata={"type": conflict_type.value}
        )
        return record

    def consolidate(self) -> None:
        by_key: dict[tuple[str, str], GovernedMemory] = {}
        inactive: dict[str, GovernedMemory] = {}
        for raw in list(self.registry.values()):
            memory = GovernedMemory(**raw)
            if memory.status in {
                MemoryStatus.STALE.value,
                MemoryStatus.DEMOTED.value,
                MemoryStatus.REJECTED.value,
            }:
                inactive[memory.memory_id] = memory
                continue
            key = (memory.scope, memory.key.lower().strip())
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = memory
                continue
            if existing.value.strip().lower() == memory.value.strip().lower():
                existing.evidence_refs = list(
                    dict.fromkeys(existing.evidence_refs + memory.evidence_refs)
                )
                existing.confidence = max(existing.confidence, memory.confidence)
                existing.verification_count += memory.verification_count
                continue
            conflict_type = self._classify_conflict(existing, memory)
            conflict = self._create_conflict(existing, memory, conflict_type)
            existing.conflict_status = ConflictStatus.PENDING.value
            existing.status = MemoryStatus.CONFLICTED.value
            existing.conflict_ids = list(
                dict.fromkeys([*existing.conflict_ids, conflict.conflict_id])
            )
            memory.conflict_status = ConflictStatus.PENDING.value
            memory.status = MemoryStatus.CONFLICTED.value
            memory.conflict_ids = list(dict.fromkeys([*memory.conflict_ids, conflict.conflict_id]))
            by_key[(memory.scope, memory.memory_id)] = memory
            self._audit("conflict", existing, conflict.conflict_id)
        self.registry = {
            **{memory_id: memory.to_dict() for memory_id, memory in inactive.items()},
            **{memory.memory_id: memory.to_dict() for memory in by_key.values()},
        }
        self.state["governed_memories"] = self.registry
        self.state["memory_conflicts"] = self.conflicts

    def refresh_versions(self) -> None:
        if not self.current_repo_fingerprint:
            return
        for raw in self.registry.values():
            if (
                raw.get("scope") == MemoryScope.REPOSITORY_VERSION.value
                and raw.get("repo_fingerprint") not in ("", self.current_repo_fingerprint)
            ):
                raw["status"] = MemoryStatus.STALE.value
                self.stats["stale_marked"] += 1
                queue = self.state.setdefault("memory_revalidation_queue", [])
                item = {
                    "memory_id": raw.get("memory_id", ""),
                    "reason": "repository_version_changed",
                    "repo_fingerprint": self.current_repo_fingerprint,
                    "queued_at": time.time(),
                }
                if not any(row.get("memory_id") == item["memory_id"] for row in queue):
                    queue.append(item)
                    self.stats["revalidation_queued"] += 1
                    if self.store:
                        self.store.enqueue_revalidation(item)
        del self.state.setdefault("memory_revalidation_queue", [])[:-100]

    def resolve_conflict(
        self,
        conflict_id: str,
        winner_ids: list[str],
        *,
        evidence_refs: list[str] | None = None,
        resolver: str = "runtime",
        reason: str = "",
        allow_multiple: bool = False,
    ) -> bool:
        """Resolve one conflict from deterministic evidence or explicit user input."""
        raw_conflict = self.conflicts.get(conflict_id)
        if not raw_conflict:
            return False
        candidates = set(raw_conflict.get("candidate_ids", []))
        winners = list(dict.fromkeys(winner_ids))
        if not winners or any(winner not in candidates for winner in winners):
            return False
        if len(winners) > 1 and not allow_multiple:
            return False
        refs = list(dict.fromkeys(evidence_refs or []))
        for memory_id in candidates:
            memory_raw = self.registry.get(memory_id)
            if not memory_raw:
                continue
            memory_raw["conflict_status"] = "none"
            memory_raw["conflict_ids"] = [
                item for item in memory_raw.get("conflict_ids", []) if item != conflict_id
            ]
            memory_raw["evidence_refs"] = list(
                dict.fromkeys([*(memory_raw.get("evidence_refs") or []), *refs])
            )
            if memory_id in winners:
                memory_raw["status"] = MemoryStatus.ACTIVE.value
                memory_raw["confidence"] = min(
                    1.0, float(memory_raw.get("confidence", 0.0)) + 0.1
                )
                memory_raw["last_verified_at"] = time.time()
                memory_raw["allow_multiple"] = len(winners) > 1
            else:
                memory_raw["status"] = (
                    MemoryStatus.STALE.value
                    if raw_conflict.get("conflict_type") == ConflictType.TEMPORAL_UPDATE.value
                    else MemoryStatus.REJECTED.value
                )
        raw_conflict.update(
            {
                "status": ConflictStatus.RESOLVED.value,
                "winner_ids": winners,
                "evidence_refs": refs,
                "resolver": resolver,
                "reason": reason,
                "resolved_at": time.time(),
            }
        )
        self.stats["conflicts_resolved"] += 1
        self._audit_control(
            "conflict_resolved",
            conflict_id,
            reason=reason,
            metadata={"winner_ids": winners, "resolver": resolver},
        )
        return True

    def _resolve_by_version(self, conflict: ConflictRecord) -> bool:
        if conflict.conflict_type != ConflictType.TEMPORAL_UPDATE.value:
            return False
        winners = [
            memory_id
            for memory_id in conflict.candidate_ids
            if self.registry.get(memory_id, {}).get("repo_fingerprint")
            == self.current_repo_fingerprint
        ]
        if len(winners) != 1:
            return False
        return self.resolve_conflict(
            conflict.conflict_id,
            winners,
            resolver="repository_version",
            reason="current repository fingerprint matched",
        )

    def _resolve_multi_value(self, conflict: ConflictRecord) -> bool:
        if conflict.conflict_type != ConflictType.MULTI_VALUE.value:
            return False
        return self.resolve_conflict(
            conflict.conflict_id,
            conflict.candidate_ids,
            resolver="multi_value_policy",
            reason="key explicitly permits multiple values",
            allow_multiple=True,
        )

    def _resolve_by_authority(self, conflict: ConflictRecord) -> bool:
        if conflict.conflict_type != ConflictType.SOURCE_DISAGREEMENT.value:
            return False
        ranked = []
        for memory_id in conflict.candidate_ids:
            raw = self.registry.get(memory_id, {})
            ranked.append(
                (
                    _AUTHORITY_RANK.get(str(raw.get("authority", "")), 0),
                    memory_id,
                    raw,
                )
            )
        ranked.sort(reverse=True)
        if len(ranked) < 2 or ranked[0][0] <= ranked[1][0]:
            return False
        _, winner_id, winner = ranked[0]
        if not winner.get("evidence_refs") and winner.get("authority") not in {
            MemoryAuthority.CURRENT_USER.value,
            MemoryAuthority.USER_CONFIRMED.value,
        }:
            return False
        return self.resolve_conflict(
            conflict.conflict_id,
            [winner_id],
            evidence_refs=list(winner.get("evidence_refs") or []),
            resolver="authority",
            reason="unique higher-authority candidate had supporting evidence",
        )

    def _resolve_by_probe(self, conflict: ConflictRecord) -> bool:
        if self.probe is None:
            return False
        candidates = [
            dict(self.registry[memory_id])
            for memory_id in conflict.candidate_ids
            if memory_id in self.registry
        ]
        self.stats["probe_attempts"] += 1
        try:
            result = self.probe(conflict, candidates)
        except Exception as exc:
            self._audit_control("conflict_probe_failed", conflict.conflict_id, reason=str(exc))
            return False
        if not isinstance(result, dict):
            return False
        winners = list(result.get("winner_ids") or [])
        refs = list(result.get("evidence_refs") or [])
        if not winners or not refs:
            return False
        return self.resolve_conflict(
            conflict.conflict_id,
            winners,
            evidence_refs=refs,
            resolver="tool_probe",
            reason=str(result.get("reason", "probe evidence")),
            allow_multiple=bool(result.get("allow_multiple", False)),
        )

    def resolve_pending_conflicts(
        self, *, required_keys: set[str] | None = None, allow_probe: bool = False
    ) -> int:
        """Resolve cheap cases eagerly and probe only conflicts needed by the task."""
        required = {key.strip().lower() for key in (required_keys or set())}
        resolved = 0
        for raw in list(self.conflicts.values()):
            if raw.get("status") == ConflictStatus.RESOLVED.value:
                continue
            conflict = ConflictRecord(**raw)
            if (
                self._resolve_by_version(conflict)
                or self._resolve_multi_value(conflict)
                or self._resolve_by_authority(conflict)
            ):
                resolved += 1
                continue
            key_required = conflict.key.strip().lower() in required
            if allow_probe and key_required and self._resolve_by_probe(conflict):
                resolved += 1
                continue
            if raw.get("status") != ConflictStatus.UNRESOLVED.value:
                raw["status"] = ConflictStatus.UNRESOLVED.value
                self.stats["conflicts_unresolved"] += 1
        return resolved

    def conflicts_for_query(self, query: str) -> list[dict[str, Any]]:
        """Return unresolved conflicts relevant to the current decision."""
        tokens = _query_tokens(query)
        return [
            dict(raw)
            for raw in self.conflicts.values()
            if raw.get("status") != ConflictStatus.RESOLVED.value
            and any(token in str(raw.get("key", "")).lower() for token in tokens)
        ]

    def resolve_for_query(self, query: str, *, allow_probe: bool = True) -> int:
        """Resolve only conflicts that could affect the current decision."""
        required_keys = {
            str(raw.get("key", "")) for raw in self.conflicts_for_query(query)
        }
        return self.resolve_pending_conflicts(
            required_keys=required_keys,
            allow_probe=allow_probe,
        )

    def promote_eligible(self, *, user_confirmed: bool = False) -> int:
        count = 0
        for raw in self.registry.values():
            memory = GovernedMemory(**raw)
            allowed, reason = can_promote(memory, user_confirmed=user_confirmed)
            if not allowed:
                if reason != "task_or_run_scope":
                    self.stats["rejected"] += 1
                continue
            memory.status = MemoryStatus.DURABLE.value
            self.registry[memory.memory_id] = memory.to_dict()
            count += 1
            self.stats["promoted"] += 1
            self._audit("promote", memory)
        return count

    def inspect(self, memory_id: str) -> dict[str, Any] | None:
        raw = self.registry.get(memory_id)
        return dict(raw) if raw else None

    def list_memories(
        self, *, status: str = "", scope: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = [
            dict(raw)
            for raw in self.registry.values()
            if (not status or raw.get("status") == status)
            and (not scope or raw.get("scope") == scope)
        ]
        rows.sort(key=lambda raw: float(raw.get("last_seen_at", 0.0)), reverse=True)
        return rows[: max(0, int(limit))]

    def list_conflicts(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        rows = [
            dict(raw)
            for raw in self.conflicts.values()
            if not status or raw.get("status") == status
        ]
        rows.sort(key=lambda raw: float(raw.get("created_at", 0.0)), reverse=True)
        return rows[: max(0, int(limit))]

    def confirm(self, memory_id: str, *, evidence_ref: str = "user:confirmed") -> bool:
        raw = self.registry.get(memory_id)
        if not raw:
            return False
        raw["authority"] = MemoryAuthority.USER_CONFIRMED.value
        raw["confidence"] = max(0.9, float(raw.get("confidence", 0.0)))
        raw["evidence_refs"] = list(
            dict.fromkeys([*(raw.get("evidence_refs") or []), evidence_ref])
        )
        raw["cross_confirmed"] = True
        raw["verification_count"] = max(1, int(raw.get("verification_count", 0)))
        raw["status"] = MemoryStatus.VERIFIED.value
        self._audit("user_confirm", GovernedMemory(**raw))
        return True

    def reject(self, memory_id: str, *, reason: str = "user_rejected") -> bool:
        raw = self.registry.get(memory_id)
        if not raw:
            return False
        raw["status"] = MemoryStatus.REJECTED.value
        self.stats["rejected"] += 1
        self._audit("user_reject", GovernedMemory(**raw), reason)
        return True

    def forget(self, memory_id: str) -> bool:
        raw = self.registry.pop(memory_id, None)
        if not raw:
            return False
        for conflict_id in list(raw.get("conflict_ids", [])):
            conflict = self.conflicts.get(conflict_id)
            if not conflict:
                continue
            conflict["candidate_ids"] = [
                item for item in conflict.get("candidate_ids", []) if item != memory_id
            ]
            conflict["winner_ids"] = [
                item for item in conflict.get("winner_ids", []) if item != memory_id
            ]
            if len(conflict["candidate_ids"]) < 2:
                self.conflicts.pop(conflict_id, None)
        recalled = self.state.get("recalled_memory_ids", [])
        self.state["recalled_memory_ids"] = [item for item in recalled if item != memory_id]
        self.state["memory_usage_events"] = [
            event
            for event in self.state.get("memory_usage_events", [])
            if event.get("memory_id") != memory_id
        ]
        self.state["memory_revalidation_queue"] = [
            item
            for item in self.state.get("memory_revalidation_queue", [])
            if item.get("memory_id") != memory_id
        ]
        self._audit_control("memory_forget", memory_id)
        return True

    def pin(self, memory_id: str, *, pinned: bool = True) -> bool:
        raw = self.registry.get(memory_id)
        if not raw:
            return False
        raw["pinned"] = bool(pinned)
        self._audit("memory_pin" if pinned else "memory_unpin", GovernedMemory(**raw))
        return True

    def user_resolve(self, conflict_id: str, winner_ids: list[str]) -> bool:
        for memory_id in winner_ids:
            if not self.confirm(memory_id, evidence_ref="user:conflict_resolution"):
                return False
        return self.resolve_conflict(
            conflict_id,
            winner_ids,
            evidence_refs=["user:conflict_resolution"],
            resolver="user",
            reason="explicit user conflict resolution",
            allow_multiple=len(winner_ids) > 1,
        )

    def demote_invalid(self) -> int:
        count = 0
        for raw in self.registry.values():
            if raw.get("status") not in {
                MemoryStatus.ACTIVE.value,
                MemoryStatus.DURABLE.value,
                MemoryStatus.STALE.value,
            }:
                continue
            if (
                raw.get("conflict_status") != "none"
                or raw.get("status") == MemoryStatus.STALE.value
            ):
                raw["status"] = MemoryStatus.DEMOTED.value
                count += 1
                self.stats["demoted"] += 1
                self._audit("demote_invalid", GovernedMemory(**raw), "stale_or_conflict")
        return count

    def recall(
        self,
        query: str,
        *,
        scope: str = "",
        limit: int = 3,
        user_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict]:
        """Recall memories within the caller's identity boundary.

        User memories require the same user id; run/task memories require the
        same task id. Repository memories remain workspace-scoped and are
        additionally checked against the current repository fingerprint.
        """
        tokens = _query_tokens(query)
        caller_user = self.user_id if user_id is None else str(user_id or "")
        caller_task = self.task_id if task_id is None else str(task_id or "")
        scored = []
        scope_weight = {
            "task": 1.0,
            "repository": 0.9,
            "repository_version": 0.85,
            "user": 0.8,
            "run": 0.2,
        }
        for raw in self.registry.values():
            if raw.get("status") in {"rejected", "conflicted", "stale", "demoted"}:
                continue
            if raw.get("scope") == MemoryScope.USER.value and raw.get("user_id") != caller_user:
                continue
            if raw.get("scope") in {MemoryScope.RUN.value, MemoryScope.TASK.value}:
                if caller_task and raw.get("task_id") != caller_task:
                    continue
            if scope and raw.get("scope") not in {scope, "repository"}:
                continue
            policy = self.effective_policy(raw.get("key", ""))
            if policy and str(policy.get("value", "")).strip().lower() != str(
                raw.get("value", "")
            ).strip().lower():
                self.stats["policy_shadowed"] += 1
                continue
            text = f"{raw.get('key', '')} {raw.get('value', '')}".lower()
            overlap = sum(1 for token in tokens if token and token in text)
            if not overlap:
                continue
            version_match = not raw.get("repo_fingerprint") or raw.get(
                "repo_fingerprint"
            ) == self.current_repo_fingerprint
            lexical = overlap / max(len(tokens), 1)
            reference_time = float(
                raw.get("last_verified_at") or raw.get("created_at", time.time())
            )
            age_days = max(0.0, (time.time() - reference_time) / 86400)
            freshness = 1.0 / (1.0 + age_days / 30.0)
            usage_total = int(raw.get("usage_successes", 0)) + int(raw.get("usage_failures", 0))
            usage_quality = (
                (int(raw.get("usage_successes", 0)) + 1)
                / (usage_total + 2)
                if usage_total
                else 0.5
            )
            score = (
                0.55 * lexical
                + 0.20 * float(raw.get("confidence", 0.0))
                + 0.10 * freshness
                + 0.10 * usage_quality
                + 0.05 * scope_weight.get(raw.get("scope", ""), 0.5)
            )
            score *= 1.0 if version_match else 0.1
            item = dict(raw)
            item["score"] = round(score, 3)
            item["freshness"] = round(freshness, 3)
            item["effective_confidence"] = round(
                float(raw.get("confidence", 0.0))
                * freshness
                * (1.0 if version_match else 0.1),
                3,
            )
            item["memory_role"] = (
                "confirmed_fact"
                if raw.get("status") in {"verified", "active", "durable"}
                and (not raw.get("tainted") or raw.get("cross_confirmed"))
                else "historical_candidate"
            )
            scored.append(item)
        scored.sort(key=lambda item: item["score"], reverse=True)
        self.stats["recall_hits"] += min(limit, len(scored))
        for item in scored[:limit]:
            item["retrieve_count"] = int(item.get("retrieve_count", 0)) + 1
            raw = self.registry.get(item["memory_id"])
            if raw:
                raw["retrieve_count"] = item["retrieve_count"]
        recalled_ids = [item["memory_id"] for item in scored[:limit]]
        self.state["recalled_memory_ids"] = recalled_ids
        self.record_recall(recalled_ids, task_id=caller_task)
        return scored[:limit]

    def record_usage(
        self,
        memory_id: str,
        *,
        outcome: str,
        evidence_refs: list[str] | None = None,
        task_id: str | None = None,
    ) -> bool:
        """Feed post-recall repair outcome back into memory quality."""
        raw = self.registry.get(memory_id)
        if not raw:
            return False
        effective_task = self.task_id if task_id is None else str(task_id or "")
        if raw.get("scope") == MemoryScope.USER.value and raw.get("user_id") != self.user_id:
            return False
        if raw.get("scope") in {MemoryScope.RUN.value, MemoryScope.TASK.value} and (
            not effective_task or raw.get("task_id") != effective_task
        ):
            return False
        normalized = str(outcome).lower().strip()
        passed = normalized in {
            "success", "verified", "fixed", "pass", "supported", "helpful"
        }
        contradicted = normalized in {"contradicted", "harmful", "rejected", "failed"}
        inconclusive = normalized in {"inconclusive", "unused", "unknown", ""}
        raw["last_seen_at"] = time.time()
        raw["evidence_refs"] = list(
            dict.fromkeys([*(raw.get("evidence_refs") or []), *(evidence_refs or [])])
        )
        if passed:
            raw["usage_successes"] = int(raw.get("usage_successes", 0)) + 1
            raw["confidence"] = min(1.0, float(raw.get("confidence", 0.0)) + 0.05)
            if normalized in {"verified", "supported", "fixed", "pass"}:
                raw["last_verified_at"] = time.time()
        elif contradicted:
            raw["usage_failures"] = int(raw.get("usage_failures", 0)) + 1
            raw["confidence"] = max(0.0, float(raw.get("confidence", 0.0)) - 0.10)
            if raw["usage_failures"] >= 3 and raw["usage_failures"] > raw["usage_successes"]:
                raw["status"] = MemoryStatus.DEMOTED.value
            if normalized == "contradicted":
                raw["conflict_status"] = "pending"
        if inconclusive:
            self.stats["usage_inconclusive"] += 1
        elif contradicted:
            self.stats["usage_contradicted"] += 1
        else:
            self.stats["usage_supported"] += 1
        event = MemoryUsageEvent(
            memory_id=memory_id,
            task_id=effective_task,
            usage="verified" if passed else "applied",
            outcome=normalized or "inconclusive",
            evidence_refs=list(evidence_refs or []),
        ).to_dict()
        self.state.setdefault("memory_usage_events", []).append(event)
        del self.state["memory_usage_events"][:-200]
        self.stats["usage_events"] += 1
        if self.store:
            self.store.append_usage_event(event)
        self._audit(
            (
                "usage_pass"
                if passed
                else "usage_contradicted"
                if contradicted
                else "usage_inconclusive"
            ),
            GovernedMemory(**raw),
        )
        self.registry[memory_id] = raw
        self._persist_memory(memory_id)
        return True

    def run(self, *, user_confirmed: bool = False) -> dict:
        self.consolidate()
        self.refresh_versions()
        self.resolve_pending_conflicts()
        self.demote_invalid()
        self.promote_eligible(user_confirmed=user_confirmed)
        self.state["memory_governance_stats"] = dict(self.stats)
        return dict(self.stats)
