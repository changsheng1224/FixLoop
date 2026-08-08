"""Canonical identity and checkpoint contracts for resumable Agent runs."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SESSION_SCHEMA_VERSION = "2.0"
CHECKPOINT_ENVELOPE_VERSION = "2.0"


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class SessionIdentity:
    session_id: str
    user_id: str = ""
    workspace_id: str = ""
    task_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    parent_run_id: str = ""
    created_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        *,
        session_id: str = "",
        user_id: str = "",
        workspace_id: str = "",
        task_id: str = "",
        run_id: str = "",
        parent_run_id: str = "",
    ) -> SessionIdentity:
        sid = session_id or "session-" + uuid.uuid4().hex[:16]
        rid = run_id or "run-" + uuid.uuid4().hex[:16]
        return cls(
            session_id=sid,
            user_id=str(user_id or ""),
            workspace_id=str(workspace_id or ""),
            task_id=str(task_id or "") or rid,
            run_id=rid,
            attempt_id="attempt-" + uuid.uuid4().hex[:16],
            parent_run_id=str(parent_run_id or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SessionIdentity:
        data = dict(raw or {})
        return cls(
            session_id=str(data.get("session_id") or data.get("id") or ""),
            user_id=str(data.get("user_id") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            attempt_id=str(data.get("attempt_id") or ""),
            parent_run_id=str(data.get("parent_run_id") or ""),
            created_at=float(data.get("created_at", time.time()) or time.time()),
        )


@dataclass
class CheckpointEnvelope:
    """Stable outer envelope; payloads remain backward-compatible dictionaries."""

    checkpoint_id: str
    sequence: int
    trigger: str
    safe_point: str
    identity: dict[str, Any]
    runtime_control: dict[str, Any] = field(default_factory=dict)
    task_state: dict[str, Any] = field(default_factory=dict)
    context_manifest: dict[str, Any] = field(default_factory=dict)
    workspace_manifest: dict[str, Any] = field(default_factory=dict)
    action_ledger: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    observation_manifest: list[dict[str, Any]] = field(default_factory=list)
    terminal_status: str = "running"
    parent_checkpoint_id: str = ""
    created_at: float = field(default_factory=time.time)
    committed_at: float = 0.0
    schema_version: str = CHECKPOINT_ENVELOPE_VERSION
    checksum: str = ""

    def payload_for_integrity(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("checksum", None)
        return data

    def seal(self) -> CheckpointEnvelope:
        self.committed_at = self.committed_at or time.time()
        self.checksum = _stable_hash(self.payload_for_integrity())
        return self

    def verify(self) -> bool:
        return bool(self.checksum) and self.checksum == _stable_hash(self.payload_for_integrity())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CheckpointEnvelope:
        fields = {
            "checkpoint_id",
            "sequence",
            "trigger",
            "safe_point",
            "identity",
            "runtime_control",
            "task_state",
            "context_manifest",
            "workspace_manifest",
            "action_ledger",
            "side_effects",
            "observation_manifest",
            "terminal_status",
            "parent_checkpoint_id",
            "created_at",
            "committed_at",
            "schema_version",
            "checksum",
        }
        data = {key: value for key, value in raw.items() if key in fields}
        return cls(
            checkpoint_id=str(data.get("checkpoint_id") or "cp-" + uuid.uuid4().hex[:16]),
            sequence=int(data.get("sequence", 0) or 0),
            trigger=str(data.get("trigger") or "ask_end"),
            safe_point=str(data.get("safe_point") or "full_session"),
            identity=dict(data.get("identity") or {}),
            runtime_control=dict(data.get("runtime_control") or {}),
            task_state=dict(data.get("task_state") or {}),
            context_manifest=dict(data.get("context_manifest") or {}),
            workspace_manifest=dict(data.get("workspace_manifest") or {}),
            action_ledger=list(data.get("action_ledger") or []),
            side_effects=list(data.get("side_effects") or []),
            observation_manifest=list(data.get("observation_manifest") or []),
            terminal_status=str(data.get("terminal_status") or "running"),
            parent_checkpoint_id=str(data.get("parent_checkpoint_id") or ""),
            created_at=float(data.get("created_at", time.time()) or time.time()),
            committed_at=float(data.get("committed_at", 0) or 0),
            schema_version=str(data.get("schema_version") or CHECKPOINT_ENVELOPE_VERSION),
            checksum=str(data.get("checksum") or ""),
        )


def workspace_manifest(root: str, *, key_files: list[str] | None = None) -> dict[str, Any]:
    """Build a content-based workspace identity without invoking Git."""
    base = Path(root).resolve()
    files: dict[str, str] = {}
    candidates = [Path(path) for path in (key_files or [])]
    if not candidates:
        try:
            candidates = [
                path.relative_to(base)
                for path in sorted(base.rglob("*"))
                if path.is_file() and ".agent" not in path.parts and ".git" not in path.parts
            ][:500]
        except OSError:
            candidates = []
    for relative in candidates:
        path = relative if relative.is_absolute() else base / relative
        try:
            if path.is_file():
                files[str(path.relative_to(base))] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            continue
    git_head = ""
    try:
        head = base / ".git" / "HEAD"
        if head.is_file():
            git_head = head.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        pass
    payload = {"root": str(base), "git_head": git_head, "files": files}
    payload["fingerprint"] = _stable_hash(payload)[:16]
    return payload


def compare_workspace_manifest(saved: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    saved_files = dict(saved.get("files") or {})
    current_files = dict(current.get("files") or {})
    stale = sorted(
        path
        for path in set(saved_files) | set(current_files)
        if saved_files.get(path) != current_files.get(path)
    )
    identity_diff = []
    for key in ("root", "git_head"):
        if saved.get(key) != current.get(key):
            identity_diff.append(key)
    return {
        "stale_files": stale,
        "identity_diff": identity_diff,
        "exact_match": not stale and not identity_diff,
    }
