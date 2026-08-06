"""Memory Governance Service.

This service governs memory lifecycle; it never decides the current repair.
Candidates are evidence-bound and scoped before they can become durable facts.
"""

from __future__ import annotations

import hashlib
import time
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
    source_type = "user" if source == "user" else "tool" if source else "model"
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
    ):
        self.state = state
        self.repo_root = repo_root
        self.user_id = str(user_id or "")
        self.task_id = str(task_id or "")
        self.current_repo_fingerprint = repository_fingerprint(repo_root) if repo_root else ""
        self.audit_log = state.setdefault("memory_governance_audit", [])
        self.registry = state.setdefault("governed_memories", {})
        self.stats = {
            "normalized": 0,
            "supported": 0,
            "verified": 0,
            "promoted": 0,
            "demoted": 0,
            "stale_marked": 0,
            "conflicts_detected": 0,
            "rejected": 0,
            "recall_hits": 0,
        }

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
        self.stats["normalized"] += 1
        self._audit("ingest", memory)
        return memory

    def bind_evidence(self, memory_id: str, refs: list[str]) -> bool:
        raw = self.registry.get(memory_id)
        if not raw:
            return False
        raw["evidence_refs"] = list(dict.fromkeys([*raw.get("evidence_refs", []), *refs]))
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
        return passed

    def consolidate(self) -> None:
        by_key: dict[tuple[str, str], GovernedMemory] = {}
        for raw in list(self.registry.values()):
            memory = GovernedMemory(**raw)
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
            existing.conflict_status = "conflicted"
            existing.status = MemoryStatus.CONFLICTED.value
            existing.conflict_ids.append(memory.memory_id)
            memory.conflict_status = "conflicted"
            memory.status = MemoryStatus.CONFLICTED.value
            self.stats["conflicts_detected"] += 1
            by_key[(memory.scope, memory.memory_id)] = memory
            self._audit("conflict", existing, memory.memory_id)
        self.registry = {memory.memory_id: memory.to_dict() for memory in by_key.values()}
        self.state["governed_memories"] = self.registry

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
            item["memory_role"] = (
                "confirmed_fact"
                if raw.get("status") in {"verified", "active", "durable"}
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
        self.state["recalled_memory_ids"] = [item["memory_id"] for item in scored[:limit]]
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
        passed = str(outcome).lower() in {"success", "verified", "fixed", "pass"}
        raw["last_seen_at"] = time.time()
        raw["evidence_refs"] = list(
            dict.fromkeys([*(raw.get("evidence_refs") or []), *(evidence_refs or [])])
        )
        if passed:
            raw["usage_successes"] = int(raw.get("usage_successes", 0)) + 1
            raw["confidence"] = min(1.0, float(raw.get("confidence", 0.0)) + 0.05)
        else:
            raw["usage_failures"] = int(raw.get("usage_failures", 0)) + 1
            raw["confidence"] = max(0.0, float(raw.get("confidence", 0.0)) - 0.10)
            if raw["usage_failures"] >= 3 and raw["usage_failures"] > raw["usage_successes"]:
                raw["status"] = MemoryStatus.DEMOTED.value
        self._audit("usage_pass" if passed else "usage_fail", GovernedMemory(**raw))
        return True

    def run(self, *, user_confirmed: bool = False) -> dict:
        self.consolidate()
        self.refresh_versions()
        self.demote_invalid()
        self.promote_eligible(user_confirmed=user_confirmed)
        self.state["memory_governance_stats"] = dict(self.stats)
        return dict(self.stats)
