"""Governed context runtime primitives.

The runtime owns selection, provenance and resume safety. It does not make
repair decisions for the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
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
    provenance: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    redacted: bool = False
    schema_version: str = "2.0"
    result_digest: str = ""
    dedup_key: str = ""
    workspace_id: str = ""
    session_id: str = ""
    run_id: str = ""
    sensitivity: str = "internal"
    lifecycle: str = "active"
    dependencies: list[str] = field(default_factory=list)
    supersedes: str = ""
    invalidation_reason: str = ""
    invalidated_at: float = 0.0
    checksum: str = ""
    blob_size: int = 0
    redaction_policy_version: str = "v2"
    error_code: str = ""
    evidence_refs: list[str] = field(default_factory=list)


OBSERVATION_ERROR_CODES = frozenset(
    {
        "",
        "metadata_corruption",
        "blob_missing",
        "blob_checksum_mismatch",
        "permission_denied",
        "stale_source",
        "schema_incompatible",
        "storage_unavailable",
        "quota_exceeded",
        "redaction_failed",
        "concurrent_conflict",
        "execution_error",
        "tool_timeout",
        "validation_error",
        "unknown",
    }
)


def normalize_observation_error(value: Any) -> str:
    """Map provider/storage errors to a bounded, model-safe taxonomy."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in OBSERVATION_ERROR_CODES:
        return text
    if "timeout" in text:
        return "tool_timeout"
    if "permission" in text or "denied" in text:
        return "permission_denied"
    if "schema" in text or "validation" in text:
        return "validation_error"
    return "unknown"


class ObservationStore:
    """Versioned, isolated and provenance-preserving observation store.

    The session dictionary remains a compatibility cache, while SQLite is the
    canonical metadata index when a workspace root is available. Raw output is
    content-addressed and written atomically. Existing callers can continue to
    use ``put`` and ``expand``; advanced callers can query and invalidate by
    dependency.
    """

    SCHEMA_VERSION = "2.0"
    REDACTION_POLICY_VERSION = "v2"
    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, state: dict[str, Any], root: str = ""):
        self.state = state
        self.root = Path(root) if root else None
        self.registry = state.setdefault("observations", {})
        self.blobs = state.setdefault("observation_blobs", {})
        self.scope = dict(state.get("session_scope") or {})
        self.workspace_id = str(self.scope.get("workspace_id", "") or "")
        if not self.workspace_id and self.root is not None:
            self.workspace_id = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        self.session_id = str(self.scope.get("session_id", state.get("id", "")) or "")
        self.run_id = str(state.get("run_id", "") or "")
        lock_key = str((self.root or Path("memory://observations")).resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(lock_key, threading.RLock())
        self._db: sqlite3.Connection | None = None
        if self.root is not None:
            try:
                db_path = self.root / ".agent" / "observations" / "observations.sqlite3"
                db_path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute(
                    """CREATE TABLE IF NOT EXISTS observations (
                        observation_id TEXT PRIMARY KEY,
                        dedup_key TEXT NOT NULL,
                        record_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        lifecycle TEXT NOT NULL,
                        workspace_id TEXT,
                        session_id TEXT,
                        run_id TEXT
                    )"""
                )
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_observation_dedup ON observations(dedup_key)"
                )
                self._db.commit()
                scope_rows = self._db.execute(
                    "SELECT record_json FROM observations "
                    "WHERE workspace_id = ? AND (session_id = ? OR ? = '')",
                    (self.workspace_id, self.session_id, self.session_id),
                ).fetchall()
                for (encoded,) in scope_rows:
                    try:
                        record = json.loads(encoded)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    oid = str(record.get("observation_id", ""))
                    if oid and oid not in self.registry:
                        self.registry[oid] = record
                        self.state.setdefault("observation_index", {})[
                            record.get("dedup_key", "")
                        ] = oid
            except (OSError, sqlite3.Error):
                self._db = None

    def close(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

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
        provenance: dict[str, Any] | None = None,
        status: str = "ok",
        redact: bool = True,
        dependencies: list[str] | None = None,
        sensitivity: str = "internal",
        error_code: str = "",
        evidence_refs: list[str] | None = None,
    ) -> Observation:
        with self._lock:
            args = dict(args or {})
            args_hash = self._args_hash(args)
            key = self._dedup_key(tool, args_hash, source_version, args)
            existing_id = self.state.setdefault("observation_index", {}).get(key)
            if existing_id and existing_id in self.registry:
                existing = self._from_record(self.registry[existing_id])
                if existing.lifecycle == "active" and not existing.stale:
                    return existing

            safe_raw, safe_summary, safe_facts, safe_provenance = self._sanitize(
                raw_text,
                summary or str(raw_text)[:500],
                structured_facts or [],
                provenance or {},
                redact,
            )
            safe_facts = self._derive_facts(str(tool), args, safe_facts, source_version, safe_raw)
            result_digest = hashlib.sha256(safe_raw.encode("utf-8", "replace")).hexdigest()
            previous_id = existing_id or ""
            seed = f"{key}:{result_digest}:{time.time_ns()}"
            observation_id = "OBS-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
            raw_ref, checksum, blob_size = self._persist_raw(
                observation_id, safe_raw, result_digest
            )
            deps = list(dependencies or self._infer_dependencies(tool, args))
            observation = Observation(
                observation_id=observation_id,
                tool=str(tool),
                args_hash=args_hash,
                source_version=str(source_version or ""),
                summary=safe_summary,
                raw_ref=raw_ref,
                structured_facts=safe_facts,
                token_cost=max(1, len(safe_summary.split())),
                provenance=safe_provenance,
                status=str(status or "ok"),
                redacted=True,
                schema_version=self.SCHEMA_VERSION,
                result_digest=result_digest,
                dedup_key=key,
                workspace_id=self.workspace_id,
                session_id=self.session_id,
                run_id=self.run_id,
                sensitivity=str(sensitivity or "internal"),
                dependencies=deps,
                supersedes=previous_id,
                checksum=checksum,
                blob_size=blob_size,
                redaction_policy_version=self.REDACTION_POLICY_VERSION,
                error_code=normalize_observation_error(error_code),
                evidence_refs=list(evidence_refs or safe_provenance.get("evidence_refs", []) or []),
            )
            record = asdict(observation)
            self.registry[observation_id] = record
            self.state.setdefault("observation_index", {})[key] = observation_id
            self._persist_record(record)
            self.state.setdefault("observation_audit", []).append(
                {"event": "created", "observation_id": observation_id, "at": time.time()}
            )
            self.state["observation_audit"] = self.state["observation_audit"][-500:]
            return observation

    @staticmethod
    def _from_record(raw: dict[str, Any]) -> Observation:
        fields = set(Observation.__dataclass_fields__)
        return Observation(**{key: value for key, value in raw.items() if key in fields})

    def _dedup_key(
        self, tool: str, args_hash: str, source_version: str, args: dict[str, Any]
    ) -> str:
        dependency = ",".join(sorted(self._infer_dependencies(tool, args)))
        scope = f"{self.workspace_id}:{self.session_id}"
        fingerprint = self._source_fingerprint(args, source_version)
        return f"{scope}:{tool}:{args_hash}:{fingerprint}:{dependency}"

    def _source_fingerprint(self, args: dict[str, Any], source_version: str) -> str:
        payload: dict[str, Any] = {"source_version": str(source_version or "")}
        if self.root is not None:
            files = {}
            for relative in self._infer_dependencies("read_file", args):
                path = (self.root / relative).resolve()
                try:
                    path.relative_to(self.root.resolve())
                    files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                except (OSError, ValueError):
                    files[relative] = "missing"
            payload["files"] = files
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]

    @staticmethod
    def _redact(text: str) -> str:
        """Best-effort central redaction before raw persistence."""
        import re

        result = str(text)
        result = re.sub(
            r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
            lambda match: f"{match.group(1)}:[REDACTED]",
            result,
        )
        return re.sub(
            r"(?i)authorization:\s*bearer\s+[^\s]+",
            "authorization:[REDACTED]",
            result,
        )

    @classmethod
    def _sanitize(cls, raw_text, summary, facts, provenance, redact):
        # ``redact`` is retained for source compatibility, but persistence is
        # always sanitized at this boundary.
        try:
            from agent_runtime.security import redact_artifact, redact_text

            safe_raw = cls._redact(redact_text(str(raw_text)))
            safe = redact_artifact({"summary": summary, "facts": facts, "provenance": provenance})
            return (
                safe_raw,
                cls._redact(str(safe["summary"])),
                cls._redact_value(list(safe["facts"])),
                cls._redact_value(dict(safe["provenance"])),
            )
        except Exception:
            safe_raw = cls._redact(str(raw_text))
            return (
                safe_raw,
                cls._redact(str(summary)),
                cls._redact_value(facts),
                cls._redact_value(provenance),
            )

    @classmethod
    def _redact_value(cls, value):
        if isinstance(value, dict):
            return {str(key): cls._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return [cls._redact_value(item) for item in value]
        if isinstance(value, str):
            return cls._redact(value)
        return value

    @staticmethod
    def _infer_dependencies(tool: str, args: dict[str, Any]) -> list[str]:
        deps = []
        for key in ("path", "filepath", "file", "target"):
            value = args.get(key)
            if isinstance(value, str) and value:
                deps.append(value)
        if str(tool).lower() in {"read_file", "grep", "search_files", "quick_test"}:
            return sorted(set(deps))
        return []

    @staticmethod
    def _derive_facts(
        tool: str, args: dict[str, Any], facts: list, source_version: str, raw: str
    ) -> list:
        derived = list(facts)
        if tool in {"read_file", "grep", "search_files", "quick_test"}:
            derived.append(
                {"kind": "source", "path": args.get("path", ""), "source_version": source_version}
            )
        if tool in {"grep", "search_files"}:
            derived.append(
                {
                    "kind": "search",
                    "pattern": args.get("pattern", ""),
                    "match_count": raw.count("\n"),
                }
            )
        if tool in {"quick_test", "sandbox_test", "run_tests"}:
            derived.append(
                {"kind": "verification", "passed": not raw.lstrip().lower().startswith("error")}
            )
        if tool in {"apply_patch", "patch_file", "write_file"}:
            derived.append({"kind": "patch", "path": args.get("path", "")})
        return derived

    def _persist_record(self, record: dict[str, Any]) -> None:
        if self._db is None:
            return
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["observation_id"],
                    record["dedup_key"],
                    json.dumps(record, ensure_ascii=False),
                    record["created_at"],
                    record["lifecycle"],
                    record["workspace_id"],
                    record["session_id"],
                    record["run_id"],
                ),
            )
            self._db.commit()
        except sqlite3.Error:
            # Session state remains the compatibility fallback; callers can inspect audit.
            self.state.setdefault("observation_audit", []).append(
                {
                    "event": "metadata_persist_failed",
                    "observation_id": record["observation_id"],
                    "at": time.time(),
                }
            )

    def mark_stale_for_version(self, source_version: str) -> int:
        return self.invalidate(
            lambda raw: bool(source_version) and raw.get("source_version") == source_version,
            "source_version_changed",
        )

    def invalidate_paths(self, paths: list[str] | None, reason: str = "dependency_changed") -> int:
        changed = {str(path) for path in paths or [] if path}
        return self.invalidate(lambda raw: bool(changed & set(raw.get("dependencies", []))), reason)

    def invalidate(self, predicate, reason: str = "invalidated") -> int:
        count = 0
        with self._lock:
            for raw in self.registry.values():
                if raw.get("lifecycle") == "active" and predicate(raw):
                    raw["stale"] = True
                    raw["lifecycle"] = "stale"
                    raw["invalidation_reason"] = reason
                    raw["invalidated_at"] = time.time()
                    self._persist_record(raw)
                    count += 1
        return count

    def expand(self, observation_id: str) -> str:
        raw = self.registry.get(observation_id)
        if not raw or not raw.get("raw_ref"):
            return ""
        if (
            raw.get("workspace_id")
            and self.workspace_id
            and raw.get("workspace_id") != self.workspace_id
        ):
            return ""
        path = Path(raw["raw_ref"])
        try:
            if raw["raw_ref"].startswith("memory:"):
                return str(self.blobs.get(observation_id, ""))
            if self.root is not None:
                path.resolve().relative_to((self.root / ".agent" / "observations").resolve())
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            if (
                raw.get("checksum")
                and hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() != raw["checksum"]
            ):
                self.invalidate(
                    lambda item: item.get("observation_id") == observation_id,
                    "blob_checksum_mismatch",
                )
                return ""
            return text
        except OSError:
            return ""
        except ValueError:
            return ""

    def get(self, observation_id: str) -> Observation | None:
        raw = self.registry.get(str(observation_id))
        return self._from_record(raw) if raw else None

    def link_evidence(self, observation_id: str, evidence_ids: list[str]) -> bool:
        raw = self.registry.get(str(observation_id))
        if not raw:
            return False
        refs = list(dict.fromkeys(list(raw.get("evidence_refs", [])) + list(evidence_ids)))
        raw["evidence_refs"] = refs
        self._persist_record(raw)
        return True

    def query(
        self, *, tool: str = "", status: str = "", lifecycle: str = "", limit: int = 50
    ) -> list[Observation]:
        rows = list(self.registry.values())
        rows = [row for row in rows if (not tool or row.get("tool") == tool)]
        rows = [row for row in rows if (not status or row.get("status") == status)]
        rows = [
            row for row in rows if (not lifecycle or row.get("lifecycle", "active") == lifecycle)
        ]
        return [
            self._from_record(row)
            for row in sorted(rows, key=lambda item: item.get("created_at", 0), reverse=True)[
                : max(0, limit)
            ]
        ]

    def gc(self, *, max_records: int = 500, now: float | None = None) -> dict[str, int]:
        cutoff = float(now or time.time())
        candidates = sorted(self.registry.values(), key=lambda item: item.get("created_at", 0))
        remove = [item for item in candidates if item.get("lifecycle") == "stale"]
        if len(self.registry) - len(remove) > max_records:
            remove.extend(candidates[: len(self.registry) - max_records])
        removed = 0
        for raw in list(remove):
            oid = raw.get("observation_id")
            if oid in self.registry:
                self.registry.pop(oid, None)
                self.blobs.pop(oid, None)
                if self._db is not None:
                    try:
                        self._db.execute(
                            "DELETE FROM observations WHERE observation_id = ?", (oid,)
                        )
                    except sqlite3.Error:
                        pass
                ref = raw.get("raw_ref", "")
                still_referenced = any(
                    other.get("raw_ref") == ref
                    for other in self.registry.values()
                    if other.get("observation_id") != oid
                )
                if ref and not ref.startswith("memory:") and not still_referenced:
                    try:
                        Path(ref).unlink(missing_ok=True)
                    except OSError:
                        pass
                removed += 1
        if self._db is not None:
            try:
                self._db.commit()
            except sqlite3.Error:
                pass
        return {"removed": removed, "remaining": len(self.registry), "at": int(cutoff)}

    def _persist_raw(
        self, observation_id: str, raw_text: str, digest: str = ""
    ) -> tuple[str, str, int]:
        if self.root is None:
            self.blobs[observation_id] = raw_text
            return (
                "memory:" + observation_id,
                hashlib.sha256(raw_text.encode()).hexdigest(),
                len(raw_text.encode()),
            )
        checksum = digest or hashlib.sha256(raw_text.encode()).hexdigest()
        path = self.root / ".agent" / "observations" / f"blob-{checksum}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{observation_id}.", suffix=".tmp", dir=str(path.parent)
            )
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                tmp.write_text(raw_text, encoding="utf-8")
                with tmp.open("rb") as handle:
                    os.fsync(handle.fileno())
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
            encoded = raw_text.encode("utf-8")
            return str(path), checksum, len(encoded)
        except OSError:
            self.blobs[observation_id] = raw_text
            encoded = raw_text.encode("utf-8")
            return "memory:" + observation_id, checksum, len(encoded)


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
    status: str = "planned"
    idempotency_key: str = ""
    receipt: str = ""
    postcondition: dict[str, Any] = field(default_factory=dict)
    uncertain_reason: str = ""


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
    idempotency_key: str = "",
    status: str = "planned",
) -> ActionRecord:
    raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=True, default=str)
    return ActionRecord(
        action_id="ACT-" + hashlib.sha256(f"{tool}:{raw}".encode()).hexdigest()[:12],
        tool=tool,
        args_hash=hashlib.sha256(raw.encode()).hexdigest()[:16],
        precondition_revision=revision,
        result_ref=result_ref,
        side_effect=side_effect,
        replay_policy=(
            "never_replay"
            if side_effect
            in {
                "write",
                "external",
                "local_write",
                "remote_write",
                "external_write",
                "destructive",
            }
            else "revalidate"
        ),
        status=status,
        idempotency_key=idempotency_key,
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
        item
        for item in state.get("action_ledger", [])
        if item.get("tool") == tool and item.get("args_hash") == args_hash
    ]
    if not matches:
        return "revalidate"
    return str(matches[-1].get("replay_policy", "revalidate"))


def action_recovery_decision(
    state: dict[str, Any],
    tool: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Return a conservative replay decision for an interrupted action."""
    raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=True, default=str)
    args_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    matches = [
        item
        for item in state.get("action_ledger", [])
        if item.get("tool") == tool and item.get("args_hash") == args_hash
    ]
    if not matches:
        return {"decision": "execute", "reason": "no_prior_action", "matches": 0}
    latest = matches[-1]
    status = str(latest.get("status", "planned"))
    policy = str(latest.get("replay_policy", "revalidate"))
    if status in {"verified", "acknowledged", "succeeded"}:
        return {"decision": "reuse", "reason": "already_verified", "action": latest}
    if status in {"dispatched", "uncertain"}:
        return {"decision": "revalidate", "reason": "side_effect_uncertain", "action": latest}
    if policy == "never_replay":
        return {"decision": "block", "reason": "never_replay", "action": latest}
    if policy == "retry_idempotent" and latest.get("idempotency_key"):
        return {"decision": "retry", "reason": "idempotent_retry", "action": latest}
    return {"decision": "revalidate", "reason": "policy_revalidate", "action": latest}
