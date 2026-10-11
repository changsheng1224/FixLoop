"""Durable SSE replay and partial answers, stored outside the S1 schema."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from src.workbench.models import SchemaError


class EventJournal:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            app = db.execute("PRAGMA application_id").fetchone()[0]
            if not version and not app:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise SchemaError("refusing to initialize unrelated event database")
                db.execute(
                    "CREATE TABLE events (run_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                    "kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL, "
                    "PRIMARY KEY (run_id, sequence))"
                )
                db.execute("PRAGMA application_id = 1179403606")
                db.execute("PRAGMA user_version = 1")
            elif version != 1 or app != 1179403606:
                raise SchemaError("unsupported workbench event database")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous = FULL")
            with db:
                yield db
        finally:
            db.close()

    def publish(self, run_id: str, kind: str, payload: dict) -> dict:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            now = time.time()
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)", (run_id, sequence, kind, encoded, now)
            )
            db.commit()
            return {"sequence": sequence, "run_id": run_id, "kind": kind, "data": payload}

    def read(self, run_id: str, after: int = 0, *, limit: int = 256) -> tuple[dict, ...]:
        with self._connect() as db:
            return tuple(
                {"sequence": seq, "run_id": run_id, "kind": kind, "data": json.loads(payload)}
                for seq, kind, payload in db.execute(
                    "SELECT sequence, kind, payload FROM events WHERE run_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (run_id, after, limit),
                )
            )

    def latest(self, run_id: str) -> int:
        with self._connect() as db:
            return db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]

    def partial_answer(self, run_id: str) -> str:
        with self._connect() as db:
            return "".join(
                json.loads(row[0])["text"]
                for row in db.execute(
                    "SELECT payload FROM events WHERE run_id=? AND kind='answer.delta' "
                    "ORDER BY sequence",
                    (run_id,),
                )
            )
