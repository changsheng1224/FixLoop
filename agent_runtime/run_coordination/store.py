"""SQLite-backed run owner and resource registry.

The registry is intentionally separate from Plan and sandbox journals.  SQLite
transactions provide the cross-process CAS for owner generation and resource
registration; the other journals remain authoritative for their own facts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from agent_runtime.plan_runtime.processes import confirmed_exited
from agent_runtime.state_root import state_root_for

from .models import OwnerLease, ResourceRecord, RunSnapshot, encode, process_owner_identity


class CoordinationError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class OwnerConflictError(CoordinationError):
    def __init__(
        self, message: str = "owner is held by another execution", *, run_status=""
    ) -> None:
        self.run_status = run_status
        super().__init__("resume_run_terminal" if run_status else "resume_owner_conflict", message)


class StaleGenerationError(CoordinationError):
    def __init__(self, message: str = "owner generation is stale") -> None:
        super().__init__("stale_generation", message)


class CoordinationIntegrityError(CoordinationError):
    def __init__(self, code: str = "coordination_integrity_failed") -> None:
        super().__init__(code)


RUN_STATES = {
    "new",
    "acquiring",
    "reconciling",
    "active",
    "cancel_requested",
    "cancelling",
    "cancelled",
    "recovery_required",
    "failed",
    "released",
}
RESOURCE_STATES = {
    "planned",
    "running",
    "completed",
    "failed",
    "cancel_requested",
    "cancelled",
    "not_started",
    "unknown",
}


def _workspace_id(workspace: str) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:32]


class RunCoordinationStore:
    SCHEMA_VERSION = 1

    def __init__(self, workspace: str, state_root: str | Path | None = None) -> None:
        self.workspace = str(Path(workspace).resolve())
        self.workspace_id = _workspace_id(self.workspace)
        root = state_root_for(self.workspace, str(state_root or "")) / ".agent" / "coordination"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root
        self.path = root / "coordination.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        deadline = time.monotonic() + 10
        while True:
            try:
                if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    conn.close()
                    raise
                time.sleep(0.01)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            schema = """
                CREATE TABLE IF NOT EXISTS coordination_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    owner_token TEXT NOT NULL,
                    owner_identity TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    cancel_request_id TEXT NOT NULL DEFAULT '',
                    coordination_revision INTEGER NOT NULL,
                    terminal_status TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_workspace
                    ON runs(workspace_id, status);
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    run_key TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    parent_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    owner_token TEXT NOT NULL,
                    receipt_ref TEXT NOT NULL,
                    cleanup TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_resources_run ON resources(run_key, status);
                CREATE TABLE IF NOT EXISTS coordination_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
            current = conn.execute(
                "SELECT value FROM coordination_meta WHERE key='schema_version'"
            ).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO coordination_meta(key,value) VALUES ('schema_version',?)",
                    (str(self.SCHEMA_VERSION),),
                )
            elif int(current["value"]) != self.SCHEMA_VERSION:
                conn.rollback()
                raise CoordinationIntegrityError("coordination_schema_unsupported")
            conn.commit()

    @property
    def run_key(self) -> str:
        run_id = getattr(self, "_current_run_id", "")
        if not run_id:
            raise CoordinationIntegrityError("run_not_bound")
        return self._key(run_id)

    def bind_run(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self._current_run_id = str(run_id)

    def _key(self, run_id: str) -> str:
        return f"{self.workspace_id}:{run_id}"

    @staticmethod
    def _token() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _lease_from(row: sqlite3.Row) -> OwnerLease:
        return OwnerLease(
            task_id=row["task_id"],
            run_id=row["run_id"],
            workspace_id=row["workspace_id"],
            owner_token=row["owner_token"],
            generation=int(row["generation"]),
            coordination_revision=int(row["coordination_revision"]),
            lease_expires_at=float(row["lease_expires_at"]),
            status=row["status"],
        )

    def _event(self, conn, run_key: str, event_type: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO coordination_events(run_key,event_type,payload,created_a"
            "t) VALUES (?,?,?,?)",
            (run_key, event_type, encode(payload), time.time()),
        )

    def acquire(
        self,
        task_id: str,
        run_id: str,
        *,
        lease_seconds: float = 30.0,
        owner_identity: dict[str, Any] | None = None,
    ) -> OwnerLease:
        now = time.time()
        expires = now + max(0.1, float(lease_seconds))
        run_key = self._key(run_id)
        identity = owner_identity or process_owner_identity()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM runs WHERE workspace_id=? AND status IN "
                "('acquiring','reconciling','active','cancel_requested','cancelli"
                "ng','recovery_required')",
                (self.workspace_id,),
            ).fetchall()
            for row in rows:
                if row["run_key"] != run_key:
                    conn.rollback()
                    raise CoordinationError("workspace_busy")
            row = conn.execute("SELECT * FROM runs WHERE run_key=?", (run_key,)).fetchone()
            if row:
                if row["task_id"] != task_id:
                    conn.rollback()
                    raise CoordinationIntegrityError("resume_task_identity_mismatch")
                if row["status"] in {"cancelled", "failed"}:
                    conn.rollback()
                    raise OwnerConflictError(
                        "run is terminal; create a new run identity", run_status=row["status"]
                    )
                live = float(row["lease_expires_at"]) > now
                if live and row["owner_token"]:
                    # A crashed process may leave a lease inside its nominal
                    # TTL.  Its durable PID identity lets a replacement owner
                    # take over immediately while preserving same-process
                    # concurrent-resume exclusion.
                    owner_identity = json.loads(row["owner_identity"] or "{}")
                    if not confirmed_exited(owner_identity):
                        conn.rollback()
                        raise OwnerConflictError()
                generation = int(row["generation"]) + 1
                token = self._token()
                revision = int(row["coordination_revision"]) + 1
                conn.execute(
                    "UPDATE runs SET task_id=?,status='reconciling',generation=?,owner_token=?,"
                    "owner_identity=?,lease_expires_at=?,coordination_revision=?,"
                    "updated_at=? WHERE run_key=?",
                    (task_id, generation, token, encode(identity), expires, revision, now, run_key),
                )
            else:
                generation, token, revision = 1, self._token(), 1
                conn.execute(
                    "INSERT INTO runs(run_key,task_id,run_id,workspace_id,workspa"
                    "ce,status,generation,"
                    "owner_token,owner_identity,lease_expires_at,coordination_rev"
                    "ision,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'reconciling',?,?,?,?,?,?,?)",
                    (
                        run_key,
                        task_id,
                        run_id,
                        self.workspace_id,
                        self.workspace,
                        generation,
                        token,
                        encode(identity),
                        expires,
                        revision,
                        now,
                        now,
                    ),
                )
            payload = {
                "task_id": task_id,
                "run_id": run_id,
                "generation": generation,
                "owner_token": token,
            }
            self._event(conn, run_key, "resume_owner_acquired", payload)
            conn.commit()
        self.bind_run(run_id)
        return OwnerLease(task_id, run_id, self.workspace_id, token, generation, revision, expires)

    def assert_request_owner(
        self,
        run_id: str,
        owner_token: str,
        generation: int,
        coordination_revision: int = 0,
    ) -> None:
        """Validate an immutable owner envelope immediately before dispatch."""
        if not owner_token or int(generation or 0) <= 0:
            raise StaleGenerationError("owner envelope is incomplete")
        with closing(self._connect()) as conn:
            row = self._row(conn, run_id)
            if (
                row["owner_token"] != owner_token
                or int(row["generation"]) != int(generation)
                or int(coordination_revision or 0) > int(row["coordination_revision"])
                or row["status"] != "active"
                or float(row["lease_expires_at"]) <= time.time()
                or row["cancel_request_id"]
            ):
                raise StaleGenerationError("dispatch owner envelope is stale")

    def _row(self, conn, run_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM runs WHERE run_key=?", (self._key(run_id),)).fetchone()
        if row is None:
            raise CoordinationIntegrityError("run_registry_missing")
        return row

    def assert_lease(self, lease: OwnerLease, *, allow_cancelling: bool = False) -> sqlite3.Row:
        now = time.time()
        with closing(self._connect()) as conn:
            row = self._row(conn, lease.run_id)
            allowed = {"active"} | (
                {"cancelling", "cancel_requested"} if allow_cancelling else set()
            )
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
                or row["workspace_id"] != lease.workspace_id
                or row["status"] not in allowed
                or float(row["lease_expires_at"]) <= now
            ):
                raise StaleGenerationError()
            return row

    def activate(self, lease: OwnerLease) -> OwnerLease:
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            if row["status"] not in {"reconciling", "cancel_requested"}:
                conn.rollback()
                raise CoordinationError("resume_activation_invalid")
            if float(row["lease_expires_at"]) <= now:
                conn.rollback()
                raise StaleGenerationError("expired owner cannot activate")
            if row["cancel_request_id"]:
                conn.rollback()
                raise CoordinationError("cancel_in_progress")
            revision = int(row["coordination_revision"]) + 1
            conn.execute(
                "UPDATE runs SET status='active',coordination_revision=?,lease_ex"
                "pires_at=?,updated_at=? WHERE run_key=?",
                (
                    revision,
                    max(float(row["lease_expires_at"]), now + 0.1),
                    self._now(),
                    self._key(lease.run_id),
                ),
            )
            self._event(
                conn, self._key(lease.run_id), "resume_activated", {"generation": lease.generation}
            )
            conn.commit()
        return OwnerLease(
            **{**lease.to_dict(), "coordination_revision": revision, "status": "active"}
        )

    @staticmethod
    def _now() -> float:
        return time.time()

    def heartbeat(self, lease: OwnerLease, *, lease_seconds: float = 30.0) -> OwnerLease:
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            if row["status"] not in {"reconciling", "active", "cancel_requested", "cancelling"}:
                conn.rollback()
                raise StaleGenerationError()
            if float(row["lease_expires_at"]) <= now:
                conn.rollback()
                raise StaleGenerationError("expired owner cannot renew")
            expires = now + max(0.1, float(lease_seconds))
            revision = int(row["coordination_revision"]) + 1
            conn.execute(
                "UPDATE runs SET lease_expires_at=?,coordination_revision=?,updat"
                "ed_at=? WHERE run_key=?",
                (expires, revision, now, self._key(lease.run_id)),
            )
            conn.commit()
        return OwnerLease(
            **{**lease.to_dict(), "lease_expires_at": expires, "coordination_revision": revision}
        )

    def request_cancel(self, lease: OwnerLease, request_id: str = "") -> str:
        request_id = request_id or f"cancel-{uuid.uuid4().hex}"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            if row["cancel_request_id"]:
                existing = row["cancel_request_id"]
                if row["status"] == "reconciling":
                    conn.execute(
                        "UPDATE runs SET status='cancel_requested',coordination_r"
                        "evision=coordination_revision+1,updated_at=? WHERE run_k"
                        "ey=?",
                        (time.time(), self._key(lease.run_id)),
                    )
                conn.commit()
                return existing
            if row["status"] in {"cancelled", "released", "failed"}:
                conn.commit()
                return request_id
            revision = int(row["coordination_revision"]) + 1
            conn.execute(
                "UPDATE runs SET status='cancel_requested',cancel_request_id=?,co"
                "ordination_revision=?,updated_at=? WHERE run_key=?",
                (request_id, revision, time.time(), self._key(lease.run_id)),
            )
            self._event(
                conn, self._key(lease.run_id), "cancel_requested", {"request_id": request_id}
            )
            conn.commit()
        return request_id

    def begin_cancelling(self, lease: OwnerLease) -> OwnerLease:
        """Close dispatch and enter the durable cancellation phase."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            if row["status"] == "cancelling":
                conn.commit()
                return OwnerLease(
                    **{
                        **lease.to_dict(),
                        "coordination_revision": int(row["coordination_revision"]),
                        "status": "cancelling",
                    }
                )
            if row["status"] != "cancel_requested":
                conn.rollback()
                raise CoordinationError("cancel_transition_invalid")
            revision = int(row["coordination_revision"]) + 1
            conn.execute(
                "UPDATE runs SET status='cancelling',coordination_revision=?,upda"
                "ted_at=? WHERE run_key=?",
                (revision, time.time(), self._key(lease.run_id)),
            )
            self._event(
                conn,
                self._key(lease.run_id),
                "cancelling_started",
                {"request_id": row["cancel_request_id"]},
            )
            conn.commit()
        return OwnerLease(
            **{**lease.to_dict(), "coordination_revision": revision, "status": "cancelling"}
        )

    def mark_recovery_required(self, lease: OwnerLease, *, error_code: str) -> RunSnapshot:
        """Fence the owner while retaining resources for a later reconcile."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            revision = int(row["coordination_revision"]) + 1
            conn.execute(
                "UPDATE runs SET status='recovery_required',terminal_status=?,own"
                "er_token='',lease_expires_at=0,coordination_revision=?,updated_a"
                "t=? WHERE run_key=?",
                (error_code, revision, time.time(), self._key(lease.run_id)),
            )
            self._event(
                conn, self._key(lease.run_id), "recovery_required", {"error_code": error_code}
            )
            conn.commit()
        return self.snapshot(lease.run_id)

    def register_resource(
        self,
        lease: OwnerLease,
        *,
        resource_id: str,
        kind: str,
        effect: str,
        parent_id: str = "",
        receipt_ref: str = "",
        payload: dict[str, Any] | None = None,
        allow_reconciling: bool = False,
    ) -> ResourceRecord:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            if row["status"] not in (
                {"active", "reconciling", "cancel_requested", "cancelling"}
                if allow_reconciling
                else {"active"}
            ) or (row["cancel_request_id"] and not allow_reconciling):
                conn.rollback()
                raise CoordinationError("dispatch_closed")
            if float(row["lease_expires_at"]) <= time.time():
                conn.rollback()
                raise StaleGenerationError()
            if (
                parent_id
                and not conn.execute(
                    "SELECT 1 FROM resources WHERE resource_id=? AND run_key=?",
                    (parent_id, self._key(lease.run_id)),
                ).fetchone()
            ):
                conn.rollback()
                raise CoordinationIntegrityError("resource_parent_missing")
            if conn.execute(
                "SELECT 1 FROM resources WHERE resource_id=?", (resource_id,)
            ).fetchone():
                conn.rollback()
                raise CoordinationError("resource_duplicate")
            now = time.time()
            record = ResourceRecord(
                resource_id,
                row["task_id"],
                row["run_id"],
                row["workspace_id"],
                parent_id,
                kind,
                effect,
                "planned",
                lease.generation,
                lease.owner_token,
                receipt_ref,
                "unverified",
                "",
                payload or {},
                now,
                now,
            )
            conn.execute(
                "INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.resource_id,
                    self._key(lease.run_id),
                    record.task_id,
                    record.run_id,
                    record.workspace_id,
                    record.parent_id,
                    record.kind,
                    record.effect,
                    record.status,
                    record.generation,
                    record.owner_token,
                    record.receipt_ref,
                    record.cleanup,
                    record.error_code,
                    encode(record.payload),
                    record.created_at,
                    record.updated_at,
                ),
            )
            self._event(conn, self._key(lease.run_id), "resource_planned", record.to_dict())
            conn.execute(
                "UPDATE runs SET coordination_revision=coordination_revision+1 WHERE run_key=?",
                (self._key(lease.run_id),),
            )
            conn.commit()
        return record

    def transition_resource(
        self,
        lease: OwnerLease,
        resource_id: str,
        status: str,
        *,
        cleanup: str = "unverified",
        receipt_ref: str = "",
        error_code: str = "",
        payload: dict[str, Any] | None = None,
        adopt_generation: bool = False,
    ) -> ResourceRecord:
        if status not in RESOURCE_STATES:
            raise ValueError("resource_status_invalid")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            if row["status"] not in {"active", "reconciling", "cancel_requested", "cancelling"}:
                conn.rollback()
                raise StaleGenerationError()
            if status in {"planned", "running"} and (
                row["status"] != "active"
                or row["cancel_request_id"]
                or float(row["lease_expires_at"]) <= time.time()
            ):
                conn.rollback()
                raise CoordinationError("dispatch_closed")
            item = conn.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
            if item is None or item["run_id"] != lease.run_id:
                conn.rollback()
                raise CoordinationIntegrityError("resource_identity_mismatch")
            adopted = False
            if int(item["generation"]) != lease.generation:
                # A replacement owner must be able to reconcile resources
                # planned by the expired generation.  This handoff is only
                # valid while the run is fenced in reconciling; active owners
                # still fail closed on every generation mismatch.
                if not (
                    adopt_generation
                    and row["status"] in {"reconciling", "cancel_requested"}
                    and int(item["generation"]) < lease.generation
                ):
                    conn.rollback()
                    raise StaleGenerationError()
                adopted = True
            now = time.time()
            conn.execute(
                "UPDATE resources SET status=?,cleanup=?,receipt_ref=?,error_code"
                "=?,payload=?,generation=?,owner_token=?,updated_at=? WHERE resou"
                "rce_id=?",
                (
                    status,
                    cleanup,
                    receipt_ref or item["receipt_ref"],
                    error_code,
                    encode({**json.loads(item["payload"]), **(payload or {})}),
                    lease.generation if adopted else int(item["generation"]),
                    lease.owner_token if adopted else item["owner_token"],
                    now,
                    resource_id,
                ),
            )
            self._event(
                conn,
                self._key(lease.run_id),
                "resource_" + status,
                {"resource_id": resource_id, "cleanup": cleanup, "error_code": error_code},
            )
            conn.execute(
                "UPDATE runs SET coordination_revision=coordination_revision+1 WHERE run_key=?",
                (self._key(lease.run_id),),
            )
            conn.commit()
            item = conn.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
        return self._resource_from(item)

    def record_event(
        self, lease: OwnerLease, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Append a coordination event under the current owner fence."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            self._event(conn, self._key(lease.run_id), event_type, payload or {})
            conn.commit()

    @staticmethod
    def _resource_from(row: sqlite3.Row) -> ResourceRecord:
        return ResourceRecord(
            resource_id=row["resource_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            workspace_id=row["workspace_id"],
            parent_id=row["parent_id"],
            kind=row["kind"],
            effect=row["effect"],
            status=row["status"],
            generation=int(row["generation"]),
            owner_token=row["owner_token"],
            receipt_ref=row["receipt_ref"],
            cleanup=row["cleanup"],
            error_code=row["error_code"],
            payload=json.loads(row["payload"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def resources(self, run_id: str) -> tuple[ResourceRecord, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM resources WHERE run_key=? ORDER BY created_at,resource_id",
                (self._key(run_id),),
            ).fetchall()
        return tuple(self._resource_from(row) for row in rows)

    def snapshot(self, run_id: str) -> RunSnapshot:
        with closing(self._connect()) as conn:
            row = self._row(conn, run_id)
        return RunSnapshot(
            row["task_id"],
            row["run_id"],
            row["workspace_id"],
            row["workspace"],
            row["status"],
            int(row["generation"]),
            row["owner_token"],
            int(row["coordination_revision"]),
            float(row["lease_expires_at"]),
            row["cancel_request_id"],
            self.resources(run_id),
        )

    def checkpoint_seal(self, lease: OwnerLease) -> dict[str, Any]:
        """Seal one atomic registry snapshot and preserve its historical watermark."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
                or row["status"] not in {"reconciling", "active", "cancel_requested", "cancelling"}
                or float(row["lease_expires_at"]) <= time.time()
            ):
                raise StaleGenerationError()
            resources = conn.execute(
                "SELECT * FROM resources WHERE run_key=? ORDER BY created_at,resource_id",
                (self._key(lease.run_id),),
            ).fetchall()
            material = [
                {
                    key: item[key]
                    for key in (
                        "resource_id",
                        "parent_id",
                        "kind",
                        "effect",
                        "status",
                        "cleanup",
                        "receipt_ref",
                        "generation",
                        "owner_token",
                    )
                }
                for item in resources
            ]
            seal = {
                "run_id": lease.run_id,
                "workspace_id": lease.workspace_id,
                "owner_token": lease.owner_token,
                "generation": lease.generation,
                "coordination_revision": int(row["coordination_revision"]),
                "resource_ref_checksum": hashlib.sha256(encode(material).encode()).hexdigest(),
                "status": row["status"],
            }
            self._event(conn, self._key(lease.run_id), "checkpoint_sealed", seal)
            conn.commit()
        return seal

    def verify_checkpoint_seal(self, lease: OwnerLease, seal: dict[str, Any]) -> None:
        if (
            seal.get("run_id") != lease.run_id
            or seal.get("workspace_id") != lease.workspace_id
            or not isinstance(seal.get("generation"), int)
            or not 0 < seal["generation"] <= lease.generation
            or not seal.get("owner_token")
            or not isinstance(seal.get("coordination_revision"), int)
        ):
            raise CoordinationIntegrityError("checkpoint_coordination_mismatch")
        events = self.events(lease.run_id)
        if not any(e["event_type"] == "checkpoint_sealed" and e["payload"] == seal for e in events):
            raise CoordinationIntegrityError("checkpoint_coordination_unverified")
        if not any(
            e["event_type"] == "resume_owner_acquired"
            and e["payload"].get("generation") == seal["generation"]
            and e["payload"].get("owner_token") == seal["owner_token"]
            for e in events
        ):
            raise CoordinationIntegrityError("checkpoint_owner_unverified")

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT event_id,event_type,payload,created_at FROM coordination_"
                "events WHERE run_key=? ORDER BY event_id",
                (self._key(run_id),),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def finish(self, lease: OwnerLease, status: str, *, error_code: str = "") -> RunSnapshot:
        if status not in {"cancelled", "failed", "released", "recovery_required"}:
            raise ValueError("terminal_status_invalid")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, lease.run_id)
            if (
                row["owner_token"] != lease.owner_token
                or int(row["generation"]) != lease.generation
            ):
                conn.rollback()
                raise StaleGenerationError()
            resources = conn.execute(
                "SELECT status,cleanup,effect FROM resources WHERE run_key=?",
                (self._key(lease.run_id),),
            ).fetchall()
            unsafe = [
                item
                for item in resources
                if item["status"] not in {"completed", "failed", "cancelled", "not_started"}
                or item["cleanup"] != "confirmed"
            ]
            if unsafe and status != "recovery_required":
                revision = int(row["coordination_revision"]) + 1
                conn.execute(
                    "UPDATE runs SET status='recovery_required',terminal_status=?,owner_token='',"
                    "lease_expires_at=0,coordination_revision=?,updated_at=? WHERE run_key=?",
                    (
                        "finish_blocked_by_residual_resources",
                        revision,
                        time.time(),
                        self._key(lease.run_id),
                    ),
                )
                self._event(
                    conn,
                    self._key(lease.run_id),
                    "recovery_required",
                    {
                        "error_code": "finish_blocked_by_residual_resources",
                        "resource_count": len(unsafe),
                    },
                )
                conn.commit()
                return self.snapshot(lease.run_id)
            revision = int(row["coordination_revision"]) + 1
            conn.execute(
                "UPDATE runs SET status=?,terminal_status=?,owner_token='',lease_"
                "expires_at=0,coordination_revision=?,updated_at=? WHERE run_key="
                "?",
                (status, error_code, revision, time.time(), self._key(lease.run_id)),
            )
            self._event(
                conn,
                self._key(lease.run_id),
                "run_" + status,
                {"generation": lease.generation, "error_code": error_code},
            )
            if status == "cancelled":
                self._event(
                    conn,
                    self._key(lease.run_id),
                    "cancel_completed",
                    {"generation": lease.generation},
                )
            conn.commit()
        return self.snapshot(lease.run_id)
