"""SQLite-backed canonical memory store.

The store is deliberately small and dependency-free.  Governance remains the
policy owner; this module owns durable facts, usage events and audit records.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


class CanonicalMemoryStore:
    def __init__(self, root: str):
        base = Path(root) / ".agent" / "memory"
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "memory.db"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revalidation_queue (
                    memory_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    queued_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)

    def upsert_memory(self, memory: dict[str, Any]) -> None:
        memory_id = str(memory.get("memory_id", ""))
        if not memory_id:
            return
        now = time.time()
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT version FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            version = int(row["version"]) + 1 if row else 1
            conn.execute(
                """INSERT INTO memories(memory_id, payload, version, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                     payload=excluded.payload,
                     version=excluded.version,
                     updated_at=excluded.updated_at""",
                (memory_id, self._json(memory), version, now),
            )

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT payload FROM memories WHERE memory_id = ?", (str(memory_id),)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_memories(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute("SELECT payload FROM memories ORDER BY updated_at DESC").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def delete_memory(self, memory_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM memories WHERE memory_id = ?", (str(memory_id),))
            conn.execute("DELETE FROM revalidation_queue WHERE memory_id = ?", (str(memory_id),))

    def append_usage_event(self, event: dict[str, Any]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO usage_events(memory_id, payload, created_at) VALUES (?, ?, ?)",
                (str(event.get("memory_id", "")), self._json(event), time.time()),
            )

    def enqueue_revalidation(self, item: dict[str, Any]) -> None:
        memory_id = str(item.get("memory_id", ""))
        if not memory_id:
            return
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO revalidation_queue(memory_id, payload, queued_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET payload=excluded.payload""",
                (memory_id, self._json(item), time.time()),
            )

    def append_audit(self, action: str, object_id: str, payload: dict[str, Any]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO audit(action, object_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (action, str(object_id), self._json(payload), time.time()),
            )
