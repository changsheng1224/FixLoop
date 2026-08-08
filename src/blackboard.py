"""Thread-safe Multi-Agent Blackboard with CAS, TTL and audit snapshots."""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

BLACKBOARD_SCHEMA_VERSION = "2.0"


@dataclass
class BlackboardEntry:
    key: str
    value: Any
    source_agent: str
    created_at: float = field(default_factory=time.time)
    ttl: float | None = None
    status: str = "accepted"
    evidence_refs: list[str] = field(default_factory=list)
    base_revision: int = 0
    revision: int = 0
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def expired(self, now: float | None = None) -> bool:
        if self.ttl is None:
            return False
        return (time.time() if now is None else now) - self.created_at > self.ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": copy.deepcopy(self.value),
            "source_agent": self.source_agent,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "base_revision": self.base_revision,
            "revision": self.revision,
            "entry_id": self.entry_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BlackboardEntry:
        return cls(
            key=str(data.get("key", "")),
            value=copy.deepcopy(data.get("value")),
            source_agent=str(data.get("source_agent", "")),
            created_at=float(data.get("created_at", time.time()) or time.time()),
            ttl=(float(data["ttl"]) if data.get("ttl") is not None else None),
            status=str(data.get("status", "accepted")),
            evidence_refs=list(data.get("evidence_refs") or []),
            base_revision=int(data.get("base_revision", 0) or 0),
            revision=int(data.get("revision", 0) or 0),
            entry_id=str(data.get("entry_id") or uuid.uuid4().hex[:12]),
        )


@dataclass(frozen=True)
class NamespacePolicy:
    prefix: str
    allowed_sources: frozenset[str] = frozenset()
    validator: Callable[[Any], bool] | None = None
    default_ttl: float | None = None


class Blackboard:
    """Shared state board.

    Writes are atomic under an RLock.  ``expected_revision`` enables per-key
    compare-and-set while the global revision remains useful for ordering.
    ``snapshot`` returns a deep, self-contained persistence payload.
    """

    def __init__(self, *, namespace_policies: list[NamespacePolicy] | None = None):
        self._entries: dict[str, BlackboardEntry] = {}
        self._conflicts: list[dict[str, Any]] = []
        self._conflict_history: list[dict[str, Any]] = []
        self._revision = 0
        self._proposals: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._namespace_policies = list(namespace_policies or [])

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def register_namespace(
        self,
        prefix: str,
        *,
        allowed_sources: set[str] | frozenset[str] | None = None,
        validator: Callable[[Any], bool] | None = None,
        default_ttl: float | None = None,
    ) -> None:
        with self._lock:
            self._namespace_policies.append(
                NamespacePolicy(
                    prefix=str(prefix),
                    allowed_sources=frozenset(allowed_sources or ()),
                    validator=validator,
                    default_ttl=default_ttl,
                )
            )

    def _policy_for(self, key: str) -> NamespacePolicy | None:
        matches = [p for p in self._namespace_policies if key.startswith(p.prefix)]
        return max(matches, key=lambda item: len(item.prefix), default=None)

    def _validate_write(self, key: str, value: Any, source_agent: str) -> tuple[bool, str]:
        policy = self._policy_for(key)
        if policy is None:
            return True, ""
        if policy.allowed_sources and source_agent not in policy.allowed_sources:
            return False, "source_not_allowed"
        if policy.validator is not None:
            try:
                if not policy.validator(value):
                    return False, "value_schema_invalid"
            except Exception:
                return False, "value_schema_invalid"
        return True, ""

    def _purge_expired_locked(self) -> None:
        now = time.time()
        expired = [key for key, entry in self._entries.items() if entry.expired(now)]
        for key in expired:
            self._entries.pop(key, None)

    def propose(
        self,
        key: str,
        value: Any,
        source_agent: str,
        *,
        evidence_refs: list[str] | None = None,
        base_revision: int | None = None,
        base_entry_revision: int | None = None,
        ttl: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            valid, reason = self._validate_write(str(key), value, str(source_agent))
            proposal = {
                "proposal_id": uuid.uuid4().hex[:12],
                "key": str(key),
                "value": copy.deepcopy(value),
                "source_agent": str(source_agent),
                "evidence_refs": list(dict.fromkeys(evidence_refs or [])),
                "base_revision": self._revision if base_revision is None else int(base_revision),
                "base_entry_revision": base_entry_revision,
                "ttl": ttl,
                "status": "proposal" if valid else "rejected",
                "reason": reason,
                "created_at": time.time(),
            }
            self._proposals[proposal["proposal_id"]] = proposal
            return copy.deepcopy(proposal)

    def merge_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._purge_expired_locked()
            proposal = copy.deepcopy(proposal)
            key = str(proposal.get("key", ""))
            existing = self._entries.get(key)
            if proposal.get("status") == "rejected":
                self._record_conflict_locked({**proposal, "status": "rejected"})
                return {**proposal, "status": "rejected"}
            expected_entry = proposal.get("base_entry_revision")
            if expected_entry is not None:
                current_entry_revision = existing.revision if existing else 0
                if int(expected_entry) != current_entry_revision:
                    conflict = {
                        **proposal,
                        "status": "stale",
                        "current_entry_revision": current_entry_revision,
                    }
                    self._record_conflict_locked(conflict)
                    return conflict
            elif int(proposal.get("base_revision", 0) or 0) != self._revision:
                conflict = {**proposal, "status": "stale", "current_revision": self._revision}
                self._record_conflict_locked(conflict)
                return conflict
            if existing and existing.value != proposal.get("value"):
                conflict = {
                    **proposal,
                    "status": "conflicted",
                    "existing_source": existing.source_agent,
                    "existing_value": copy.deepcopy(existing.value),
                }
                self._record_conflict_locked(conflict)
                return conflict
            if existing and existing.value == proposal.get("value"):
                self._revision += 1
                existing.evidence_refs = list(
                    dict.fromkeys(
                        existing.evidence_refs + list(proposal.get("evidence_refs") or [])
                    )
                )
                existing.revision = self._revision
                accepted = {**proposal, "status": "accepted", "revision": self._revision}
                self._proposals.pop(proposal.get("proposal_id", ""), None)
                return accepted
            self._revision += 1
            policy = self._policy_for(key)
            ttl = proposal.get("ttl")
            if ttl is None and policy is not None:
                ttl = policy.default_ttl
            entry = BlackboardEntry(
                key=key,
                value=proposal.get("value"),
                source_agent=str(proposal.get("source_agent", "")),
                ttl=ttl,
                base_revision=int(proposal.get("base_revision", 0) or 0),
                revision=self._revision,
                entry_id=str(proposal.get("proposal_id", "")),
                evidence_refs=list(proposal.get("evidence_refs") or []),
            )
            self._entries[key] = entry
            accepted = {**proposal, "status": "accepted", "revision": self._revision}
            self._proposals.pop(proposal.get("proposal_id", ""), None)
            return accepted

    def _record_conflict_locked(self, conflict: dict[str, Any]) -> None:
        item = {
            "conflict_id": uuid.uuid4().hex[:12],
            "detected_at": time.time(),
            "lifecycle": "pending",
            **copy.deepcopy(conflict),
        }
        self._conflicts.append(item)
        self._conflict_history.append(copy.deepcopy(item))

    def write(
        self,
        key: str,
        value: Any,
        source_agent: str,
        ttl: float | None = None,
        *,
        expected_revision: int | None = None,
        evidence_refs: list[str] | None = None,
    ) -> bool:
        with self._lock:
            self._purge_expired_locked()
            key = str(key)
            source_agent = str(source_agent)
            valid, _reason = self._validate_write(key, value, source_agent)
            if not valid:
                self._record_conflict_locked(
                    {"key": key, "source_agent": source_agent, "value": value, "status": "rejected"}
                )
                return False
            existing = self._entries.get(key)
            current_entry_revision = existing.revision if existing else 0
            if expected_revision is not None and int(expected_revision) != current_entry_revision:
                self._record_conflict_locked(
                    {
                        "key": key,
                        "source_agent": source_agent,
                        "status": "stale",
                        "expected_entry_revision": int(expected_revision),
                        "current_entry_revision": current_entry_revision,
                    }
                )
                return False
            if existing and existing.source_agent != source_agent:
                self._record_conflict_locked(
                    {
                        "key": key,
                        "sources": [existing.source_agent, source_agent],
                        "values": [copy.deepcopy(existing.value), copy.deepcopy(value)],
                        "key_revision": current_entry_revision,
                    }
                )
                return False
            policy = self._policy_for(key)
            if ttl is None and policy is not None:
                ttl = policy.default_ttl
            self._revision += 1
            self._entries[key] = BlackboardEntry(
                key=key,
                value=copy.deepcopy(value),
                source_agent=source_agent,
                ttl=ttl,
                evidence_refs=list(dict.fromkeys(evidence_refs or [])),
                base_revision=current_entry_revision,
                revision=self._revision,
                entry_id=existing.entry_id if existing else uuid.uuid4().hex[:12],
            )
            return True

    def read(self, key: str) -> Any:
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(str(key))
            return copy.deepcopy(entry.value) if entry else None

    def read_related(self, prefix: str) -> dict[str, Any]:
        with self._lock:
            self._purge_expired_locked()
            return {
                key: copy.deepcopy(entry.value)
                for key, entry in self._entries.items()
                if key.startswith(str(prefix))
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._purge_expired_locked()
            return {
                "schema_version": BLACKBOARD_SCHEMA_VERSION,
                "entries": {
                    key: copy.deepcopy(entry.value) for key, entry in self._entries.items()
                },
                "entry_records": [entry.to_dict() for entry in self._entries.values()],
                "conflicts": copy.deepcopy(self._conflicts),
                "conflict_history": copy.deepcopy(self._conflict_history),
                "revision": self._revision,
                "proposals": copy.deepcopy(list(self._proposals.values())),
            }

    def restore_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        """Restore a complete snapshot without generating new revisions."""
        data = snapshot or {}
        version = str(data.get("schema_version", "1.0") or "1.0")
        if version not in {"1.0", BLACKBOARD_SCHEMA_VERSION}:
            raise ValueError(f"unsupported Blackboard schema_version: {version}")
        with self._lock:
            records = data.get("entry_records") or []
            if not records:
                records = [
                    {
                        "key": key,
                        "value": value,
                        "source_agent": "restored",
                        "revision": index,
                    }
                    for index, (key, value) in enumerate((data.get("entries") or {}).items(), 1)
                ]
            self._entries = {
                entry.key: entry
                for entry in (BlackboardEntry.from_dict(item) for item in records)
                if entry.key and not entry.expired()
            }
            self._conflicts = copy.deepcopy(data.get("conflicts") or [])
            self._conflict_history = copy.deepcopy(
                data.get("conflict_history") or self._conflicts
            )
            self._revision = max(
                int(data.get("revision", 0) or 0),
                max((entry.revision for entry in self._entries.values()), default=0),
            )
            self._proposals = {
                str(item.get("proposal_id")): copy.deepcopy(item)
                for item in data.get("proposals", [])
                if item.get("proposal_id")
            }

    @contextmanager
    def transaction(self) -> Iterator[Blackboard]:
        before = self.snapshot()
        try:
            yield self
        except Exception:
            self.restore_snapshot(before)
            raise

    def proposals(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._proposals.values()))

    def resolve_conflict(self, key: str, winner_source: str) -> None:
        with self._lock:
            remaining = []
            for conflict in self._conflicts:
                if conflict.get("key") != key:
                    remaining.append(conflict)
                    continue
                sources = set(conflict.get("sources") or [])
                existing_source = conflict.get("existing_source")
                if sources and winner_source not in sources and winner_source not in {
                    "merge",
                    "manual",
                }:
                    remaining.append(conflict)
                    continue
                if existing_source and not sources and winner_source not in {
                    existing_source,
                    conflict.get("source_agent"),
                    "merge",
                    "manual",
                }:
                    remaining.append(conflict)
                    continue
                resolved = {
                    **copy.deepcopy(conflict),
                    "lifecycle": "resolved",
                    "resolved_at": time.time(),
                }
                resolved["winner_source"] = winner_source
                self._conflict_history.append(resolved)
            self._conflicts = remaining

    def apply_conflict_winner(self, key: str, value: Any, winner_source: str) -> None:
        with self._lock:
            self._purge_expired_locked()
            current = self._entries.get(str(key))
            self._revision += 1
            self._entries[str(key)] = BlackboardEntry(
                key=str(key),
                value=copy.deepcopy(value),
                source_agent=str(winner_source),
                base_revision=current.revision if current else 0,
                revision=self._revision,
                entry_id=current.entry_id if current else uuid.uuid4().hex[:12],
            )
            self.resolve_conflict(str(key), str(winner_source))

    def reject_conflict(self, key: str, *, reason: str = "") -> None:
        """Close conflicts without materializing a winner."""
        with self._lock:
            remaining = []
            for conflict in self._conflicts:
                if conflict.get("key") != key:
                    remaining.append(conflict)
                    continue
                self._conflict_history.append(
                    {
                        **copy.deepcopy(conflict),
                        "lifecycle": "rejected",
                        "reason": reason,
                        "resolved_at": time.time(),
                    }
                )
            self._conflicts = remaining

    @property
    def conflicts(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._conflicts)
