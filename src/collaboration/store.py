"""SQLite-backed collaboration task, handoff and event store."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from src.collaboration.contracts import AgentResult, AgentTask, Handoff, TaskStatus


class LeaseConflictError(RuntimeError):
    pass


class CollaborationStore:
    SCHEMA_VERSION = 1

    def __init__(self, repo_root: str):
        root = Path(repo_root) / ".agent"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "collaboration.db"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def _initialize(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_records (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_ready
                    ON task_records(run_id, status, priority, updated_at);
                CREATE TABLE IF NOT EXISTS handoff_records (
                    handoff_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collaboration_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collaboration_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effect_receipts (
                    idempotency_key TEXT PRIMARY KEY,
                    effect_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO collaboration_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(self.SCHEMA_VERSION),),
            )

    def _event(self, conn, run_id: str, object_id: str, event_type: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO collaboration_events("
            "run_id, object_id, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(run_id), str(object_id), str(event_type), self._json(payload), time.time()),
        )

    def create_task(self, task: AgentTask) -> AgentTask:
        errors = task.validate()
        if errors:
            raise ValueError("; ".join(errors))
        now = time.time()
        task.created_at = task.created_at or now
        task.updated_at = now
        with closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT payload FROM task_records WHERE task_id = ?", (task.task_id,)
            ).fetchone()
            if existing:
                return AgentTask.from_dict(json.loads(existing["payload"]))
            task.version = 1
            payload = task.to_dict()
            conn.execute(
                "INSERT INTO task_records("
                "task_id, run_id, payload, version, status, priority, "
                "lease_owner, lease_expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    task.run_id,
                    self._json(payload),
                    task.version,
                    task.status.value,
                    task.priority,
                    "",
                    0.0,
                    now,
                ),
            )
            self._event(conn, task.run_id, task.task_id, "task_created", payload)
        return task

    def get_task(self, task_id: str) -> AgentTask | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT payload FROM task_records WHERE task_id = ?", (str(task_id),)
            ).fetchone()
        return AgentTask.from_dict(json.loads(row["payload"])) if row else None

    def list_tasks(self, run_id: str = "") -> list[AgentTask]:
        with closing(self._connect()) as conn, conn:
            if run_id:
                rows = conn.execute(
                    "SELECT payload FROM task_records WHERE run_id = ? "
                    "ORDER BY priority DESC, updated_at, task_id",
                    (str(run_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM task_records ORDER BY priority DESC, updated_at, task_id"
                ).fetchall()
        return [AgentTask.from_dict(json.loads(row["payload"])) for row in rows]

    def claim_task(self, task_id: str, worker: str, *, lease_seconds: float = 60.0) -> AgentTask:
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload, version, status, lease_owner, lease_expires_at "
                "FROM task_records WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(task_id)
            task = AgentTask.from_dict(json.loads(row["payload"]))
            lease_expired = float(row["lease_expires_at"] or 0.0) <= now
            if task.status not in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}:
                conn.rollback()
                raise LeaseConflictError(f"task is not claimable: {task.status.value}")
            if task.retry_at and now < task.retry_at:
                conn.rollback()
                raise LeaseConflictError("task retry backoff is active")
            if task.deadline_at and now >= task.deadline_at:
                conn.rollback()
                raise LeaseConflictError("task deadline has expired")
            if (
                task.status == TaskStatus.RUNNING
                and not lease_expired
                and row["lease_owner"] != worker
            ):
                conn.rollback()
                raise LeaseConflictError("task lease is held by another worker")
            task.status = TaskStatus.RUNNING
            task.attempt += 1
            task.lease_owner = str(worker)
            task.lease_expires_at = now + max(0.1, float(lease_seconds))
            task.version = int(row["version"]) + 1
            task.updated_at = now
            conn.execute(
                "UPDATE task_records SET payload=?, version=?, status=?, "
                "lease_owner=?, lease_expires_at=?, updated_at=? WHERE task_id=?",
                (
                    self._json(task.to_dict()),
                    task.version,
                    task.status.value,
                    task.lease_owner,
                    task.lease_expires_at,
                    now,
                    task.task_id,
                ),
            )
            self._event(conn, task.run_id, task.task_id, "task_claimed", task.to_dict())
            conn.commit()
        return task

    def heartbeat(self, task_id: str, worker: str, *, lease_seconds: float = 60.0) -> AgentTask:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != TaskStatus.RUNNING or task.lease_owner != worker:
            raise LeaseConflictError("worker does not own task lease")
        if task.lease_expires_at and task.lease_expires_at <= time.time():
            raise LeaseConflictError("task lease has expired")
        task.lease_expires_at = time.time() + max(0.1, float(lease_seconds))
        task.updated_at = time.time()
        self._replace_task(task, worker=worker, event_type="task_heartbeat")
        return task

    def complete_task(self, task_id: str, result: AgentResult, *, worker: str) -> AgentTask:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != TaskStatus.RUNNING or task.lease_owner != worker:
            raise LeaseConflictError("worker does not own task lease")
        if task.lease_expires_at and task.lease_expires_at <= time.time():
            raise LeaseConflictError("task lease has expired")
        if result.task_id != task_id:
            raise ValueError("result task_id mismatch")
        task.status = result.status
        if result.status == TaskStatus.FAILED and task.attempt < task.max_attempts:
            task.status = TaskStatus.READY
            task.retry_at = time.time() + min(60.0, float(2 ** max(0, task.attempt - 1)))
        else:
            task.retry_at = 0.0
        task.payload = {**task.payload, "result": result.to_dict()}
        task.lease_owner = ""
        task.lease_expires_at = 0.0
        task.updated_at = time.time()
        self._replace_task(task, worker=worker, event_type="task_completed")
        return task

    def _replace_task(self, task: AgentTask, *, worker: str, event_type: str) -> None:
        expected_version = task.version
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version, lease_owner FROM task_records WHERE task_id = ?", (task.task_id,)
            ).fetchone()
            if row is None or int(row["version"]) != expected_version or (
                worker and row["lease_owner"] != worker
            ):
                conn.rollback()
                raise LeaseConflictError("task changed while updating")
            task.version = int(row["version"]) + 1
            result = conn.execute(
                "UPDATE task_records SET payload=?, version=?, status=?, "
                "lease_owner=?, lease_expires_at=?, updated_at=? "
                "WHERE task_id=? AND version=? AND lease_owner=?",
                (
                    self._json(task.to_dict()),
                    task.version,
                    task.status.value,
                    task.lease_owner,
                    task.lease_expires_at,
                    task.updated_at,
                    task.task_id,
                    expected_version,
                    worker,
                ),
            )
            if result.rowcount != 1:
                conn.rollback()
                raise LeaseConflictError("task changed while updating")
            self._event(conn, task.run_id, task.task_id, event_type, task.to_dict())
            conn.commit()

    def save_handoff(self, handoff) -> None:
        errors = handoff.validate()
        if errors:
            raise ValueError("; ".join(errors))
        payload = handoff.to_dict()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO handoff_records(handoff_id, task_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(handoff_id) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (handoff.handoff_id, handoff.task_id, self._json(payload), time.time()),
            )
            self._event(conn, "", handoff.handoff_id, "handoff_updated", payload)

    def get_handoff(self, handoff_id: str):
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT payload FROM handoff_records WHERE handoff_id = ?", (str(handoff_id),)
            ).fetchone()
        return Handoff.from_dict(json.loads(row["payload"])) if row else None

    def save_receipt(self, receipt) -> None:
        payload = receipt.to_dict()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO effect_receipts(idempotency_key, effect_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(idempotency_key) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (receipt.idempotency_key, receipt.effect_id, self._json(payload), time.time()),
            )
            self._event(conn, "", receipt.effect_id, "effect_receipt_updated", payload)

    def get_receipt(self, idempotency_key: str):
        from src.collaboration.effects import EffectReceipt

        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT payload FROM effect_receipts WHERE idempotency_key = ?",
                (str(idempotency_key),),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"])
        return EffectReceipt(**data)

    def events(self, *, run_id: str = "", object_id: str = "") -> list[dict[str, Any]]:
        clauses, params = [], []
        if run_id:
            clauses.append("run_id = ?")
            params.append(str(run_id))
        if object_id:
            clauses.append("object_id = ?")
            params.append(str(object_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT event_id, run_id, object_id, event_type, payload, created_at "
                f"FROM collaboration_events{where} ORDER BY event_id",
                params,
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "object_id": row["object_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
