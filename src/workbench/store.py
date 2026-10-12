"""Transactional SQLite storage for immutable history and mutable branch cursors.

Each operation owns a short-lived connection. BEGIN IMMEDIATE serializes writers;
optional revision checks let clients reject stale actions rather than rebase them.
File snapshots are references only: this module never reads or restores a project.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.workbench.models import (
    Branch,
    ConflictError,
    Conversation,
    ConversationTree,
    Node,
    NotFoundError,
    Run,
    SchemaError,
    Snapshot,
    TerminalStatus,
    Turn,
    TurnMode,
)
from src.workbench.schema import APPLICATION_ID, IMMUTABLE_TABLES, SCHEMA_VERSION, TABLES

_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_MODES = frozenset({"chat", "project_chat", "repair"})


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _json(value: dict[str, Any] | None) -> str:
    if value is not None and not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    return json.dumps(value or {}, ensure_ascii=False, allow_nan=False)


def _record(model, row: sqlite3.Row):
    data = dict(row)
    for key in ("manifest", "artifacts"):
        if key in data:
            data[key] = json.loads(data[key])
    return model(**data)


class WorkbenchStore:
    """Explicit on-disk database; safe to reopen and share across local callers."""

    def __init__(self, database: str | Path):
        if str(database) == ":memory:":
            raise ValueError("workbench persistence requires an on-disk database")
        self.database = Path(database).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            if version == 0 and application == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone():
                    raise SchemaError("refusing to initialize a non-workbench database")
                for statement in TABLES:
                    db.execute(statement)
                for table in IMMUTABLE_TABLES:
                    for operation in ("UPDATE", "DELETE"):
                        db.execute(
                            f"CREATE TRIGGER {table}_{operation.lower()}_immutable "
                            f"BEFORE {operation} ON {table} "
                            "BEGIN SELECT RAISE(ABORT, 'history is immutable'); END"
                        )
                db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or application != APPLICATION_ID:
                raise SchemaError(f"unsupported workbench schema: {application}/{version}")
            db.commit()
            db.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = FULL")
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()

    @staticmethod
    def _get(db, table: str, identity: str, conversation_id: str | None = None):
        query = f"SELECT * FROM {table} WHERE id = ?"
        args = [identity]
        if conversation_id is not None:
            query += " AND conversation_id = ?"
            args.append(conversation_id)
        row = db.execute(query, args).fetchone()
        if row is None:
            raise NotFoundError(f"{table}: {identity}")
        return row

    def _conversation(self, db, identity: str, revision: int | None = None):
        row = self._get(db, "conversations", identity)
        if revision is not None and row["revision"] != revision:
            raise ConflictError(f"stale conversation revision: expected {revision}")
        return row

    @staticmethod
    def _idle(db, conversation_id: str) -> None:
        if db.execute(
            "SELECT 1 FROM runs WHERE conversation_id = ? AND status IN ('queued', 'running')",
            (conversation_id,),
        ).fetchone():
            raise ConflictError("finish or cancel the active turn before changing history")

    @staticmethod
    def _touch(db, conversation_id: str) -> None:
        db.execute(
            "UPDATE conversations SET revision = revision + 1, updated_at = ? WHERE id = ?",
            (time.time(), conversation_id),
        )

    def create_conversation(
        self,
        title: str = "新对话",
        *,
        project_path: str | None = None,
        baseline: tuple[str, dict] | None = None,
    ) -> Conversation:
        _text(title, "title")
        if project_path is not None:
            _text(project_path, "project_path")
            project_path = str(Path(project_path).expanduser().resolve())
        identity, branch_id, now = _id("conversation"), _id("branch"), time.time()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, NULL, ?, NULL, 0, ?, ?)",
                (identity, title, project_path, branch_id, now, now),
            )
            db.execute(
                "INSERT INTO branches VALUES (?, ?, ?, NULL, NULL, ?)",
                (branch_id, identity, "main", now),
            )
            if baseline is not None:
                if project_path is None:
                    raise ValueError("baseline requires a project")
                snapshot = self._insert_snapshot(db, identity, *baseline)
                db.execute(
                    "UPDATE conversations SET base_snapshot_id = ? WHERE id = ?",
                    (snapshot, identity),
                )
            return _record(Conversation, self._conversation(db, identity))

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self._transaction(write=False) as db:
            return _record(Conversation, self._conversation(db, conversation_id))

    def list_conversations(self) -> tuple[Conversation, ...]:
        with self._transaction(write=False) as db:
            return tuple(
                _record(Conversation, row)
                for row in db.execute("SELECT * FROM conversations ORDER BY updated_at DESC, id")
            )

    def rename_conversation(
        self, conversation_id: str, title: str, *, expected_revision: int | None = None
    ) -> Conversation:
        _text(title, "title")
        with self._transaction() as db:
            self._conversation(db, conversation_id, expected_revision)
            db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
            self._touch(db, conversation_id)
            return _record(Conversation, self._conversation(db, conversation_id))

    def get_tree(self, conversation_id: str) -> ConversationTree:
        """Return all branches, including abandoned futures, in one read transaction."""
        with self._transaction(write=False) as db:
            conversation = _record(Conversation, self._conversation(db, conversation_id))
            collections = []
            for table, model in (
                ("branches", Branch),
                ("nodes", Node),
                ("runs", Run),
                ("snapshots", Snapshot),
            ):
                collections.append(
                    tuple(
                        _record(model, row)
                        for row in db.execute(
                            f"SELECT * FROM {table} WHERE conversation_id = ? "
                            "ORDER BY created_at, id",
                            (conversation_id,),
                        )
                    )
                )
            return ConversationTree(conversation, *collections)

    def _path(self, db, conversation_id: str, node_id: str | None) -> list[Node]:
        path = []
        while node_id is not None:
            node = _record(Node, self._get(db, "nodes", node_id, conversation_id))
            path.append(node)
            node_id = node.parent_id
        return list(reversed(path))

    def get_path(self, conversation_id: str, node_id: str | None) -> tuple[Node, ...]:
        """The ancestor path through a chosen node; None denotes the empty root."""
        with self._transaction(write=False) as db:
            self._conversation(db, conversation_id)
            return tuple(self._path(db, conversation_id, node_id))

    def get_branch_path(self, conversation_id: str, branch_id: str) -> tuple[Node, ...]:
        with self._transaction(write=False) as db:
            self._conversation(db, conversation_id)
            branch = self._get(db, "branches", branch_id, conversation_id)
            return tuple(self._path(db, conversation_id, branch["head_node_id"]))

    def get_messages(self, conversation_id: str, node_id: str | None) -> tuple[dict[str, str], ...]:
        """Chat projection only. Runtime memory and tool evidence are not copied."""
        with self._transaction(write=False) as db:
            self._conversation(db, conversation_id)
            messages = []
            for node in self._path(db, conversation_id, node_id):
                if node.kind != "turn":
                    continue
                messages.append({"role": "user", "content": node.user_text})
                run = db.execute("SELECT * FROM runs WHERE node_id = ?", (node.id,)).fetchone()
                if run["status"] in _TERMINAL and run["answer"]:
                    messages.append({"role": "assistant", "content": run["answer"]})
            return tuple(messages)

    def _effective_snapshot(self, db, conversation_id: str, node_id: str | None) -> str | None:
        for node in reversed(self._path(db, conversation_id, node_id)):
            if node.kind == "local_checkpoint":
                return node.checkpoint_snapshot_id
            run = db.execute("SELECT output_snapshot_id FROM runs WHERE node_id = ?", (node.id,))
            output = run.fetchone()[0]
            if output is not None:
                return output
            if node.input_snapshot_id is not None:
                return node.input_snapshot_id
        return self._conversation(db, conversation_id)["base_snapshot_id"]

    def get_effective_snapshot(self, conversation_id: str, node_id: str | None) -> str | None:
        """State after a node, including partial output of failed/cancelled runs."""
        with self._transaction(write=False) as db:
            self._conversation(db, conversation_id)
            return self._effective_snapshot(db, conversation_id, node_id)

    def view_node(
        self, conversation_id: str, node_id: str | None, *, expected_revision: int | None = None
    ) -> Conversation:
        """Browse any historical node without changing the execution branch."""
        with self._transaction() as db:
            self._conversation(db, conversation_id, expected_revision)
            if node_id is not None:
                self._get(db, "nodes", node_id, conversation_id)
            db.execute(
                "UPDATE conversations SET view_node_id = ? WHERE id = ?", (node_id, conversation_id)
            )
            self._touch(db, conversation_id)
            return _record(Conversation, self._conversation(db, conversation_id))

    def switch_branch(
        self, conversation_id: str, branch_id: str, *, expected_revision: int | None = None
    ) -> Branch:
        """Select a stored branch head. Files remain the responsibility of S3."""
        with self._transaction() as db:
            self._conversation(db, conversation_id, expected_revision)
            self._idle(db, conversation_id)
            branch = self._get(db, "branches", branch_id, conversation_id)
            self._select(db, conversation_id, branch_id, branch["head_node_id"])
            return _record(Branch, branch)

    def _select(self, db, conversation_id: str, branch_id: str, head: str | None) -> None:
        db.execute(
            "UPDATE conversations SET active_branch_id = ?, view_node_id = ? WHERE id = ?",
            (branch_id, head, conversation_id),
        )
        self._touch(db, conversation_id)

    def _fork(self, db, conversation_id: str, node_id: str | None, name: str) -> Branch:
        _text(name, "branch name")
        if node_id is not None:
            self._get(db, "nodes", node_id, conversation_id)
        branch_id = _id("branch")
        db.execute(
            "INSERT INTO branches VALUES (?, ?, ?, ?, ?, ?)",
            (branch_id, conversation_id, name, node_id, node_id, time.time()),
        )
        return _record(Branch, self._get(db, "branches", branch_id, conversation_id))

    def fork_at(
        self,
        conversation_id: str,
        node_id: str | None,
        *,
        name: str = "新分支",
        expected_revision: int | None = None,
    ) -> Branch:
        """Continue after a historical node, preserving every existing branch."""
        with self._transaction() as db:
            self._conversation(db, conversation_id, expected_revision)
            self._idle(db, conversation_id)
            branch = self._fork(db, conversation_id, node_id, name)
            self._select(db, conversation_id, branch.id, node_id)
            return branch

    def _validate_mode(self, conversation, mode: str) -> None:
        if mode not in _MODES:
            raise ValueError(f"invalid turn mode: {mode}")
        if mode != "chat" and conversation["project_path"] is None:
            raise ValueError("project_chat and repair require a project")

    def _snapshot_ref(self, db, conversation, snapshot_id: str | None) -> None:
        if snapshot_id is not None:
            if conversation["project_path"] is None:
                raise ValueError("snapshot references require a project")
            self._get(db, "snapshots", snapshot_id, conversation["id"])

    def _append_turn(
        self, db, conversation, branch, user_text: str, mode: str, snapshot_id: str | None
    ) -> Turn:
        _text(user_text, "user_text")
        self._validate_mode(conversation, mode)
        self._snapshot_ref(db, conversation, snapshot_id)
        node_id, run_id, now = _id("node"), _id("run"), time.time()
        db.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, 'turn', ?, ?, ?, NULL, ?)",
            (
                node_id,
                conversation["id"],
                branch["head_node_id"],
                user_text,
                mode,
                snapshot_id,
                now,
            ),
        )
        db.execute(
            "INSERT INTO runs (id, conversation_id, node_id, status, created_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (run_id, conversation["id"], node_id, now),
        )
        db.execute("UPDATE branches SET head_node_id = ? WHERE id = ?", (node_id, branch["id"]))
        self._select(db, conversation["id"], branch["id"], node_id)
        return Turn(
            _record(Node, self._get(db, "nodes", node_id)),
            _record(Run, self._get(db, "runs", run_id)),
        )

    def append_turn(
        self,
        conversation_id: str,
        user_text: str,
        *,
        mode: TurnMode = "chat",
        input_snapshot_id: str | None = None,
        expected_revision: int | None = None,
    ) -> Turn:
        with self._transaction() as db:
            conversation = self._conversation(db, conversation_id, expected_revision)
            self._idle(db, conversation_id)
            branch = self._get(db, "branches", conversation["active_branch_id"], conversation_id)
            snapshot = input_snapshot_id or self._effective_snapshot(
                db, conversation_id, branch["head_node_id"]
            )
            return self._append_turn(db, conversation, branch, user_text, mode, snapshot)

    def edit_turn(
        self,
        conversation_id: str,
        node_id: str,
        user_text: str,
        *,
        name: str = "编辑分支",
        expected_revision: int | None = None,
    ) -> Turn:
        """Replace a request on a new branch, using its original pre-execution state."""
        _text(user_text, "user_text")
        return self._repeat_turn(conversation_id, node_id, user_text, name, expected_revision)

    def append_project_turn(
        self,
        conversation_id,
        user_text,
        *,
        mode,
        key,
        manifest,
        manual=False,
        expected_revision=None,
    ):
        """Capture the actual input and manual checkpoint with admission atomically."""
        with self._transaction() as db:
            conversation = self._conversation(db, conversation_id, expected_revision)
            self._idle(db, conversation_id)
            self._validate_mode(conversation, mode)
            _text(user_text, "user_text")
            snapshot = self._insert_snapshot(db, conversation_id, key, manifest)
            bid = conversation["active_branch_id"]
            if manual:
                self._checkpoint(db, conversation_id, bid, snapshot, "本地修改")
            branch = self._get(db, "branches", bid, conversation_id)
            return self._append_turn(db, conversation, branch, user_text, mode, snapshot)

    def regenerate_turn(
        self,
        conversation_id: str,
        node_id: str,
        *,
        name: str = "重跑分支",
        expected_revision: int | None = None,
    ) -> Turn:
        """Reexecute the same request as a sibling node; never overwrite a result."""
        return self._repeat_turn(conversation_id, node_id, None, name, expected_revision)

    def _repeat_turn(self, conversation_id, node_id, user_text, name, expected_revision) -> Turn:
        with self._transaction() as db:
            conversation = self._conversation(db, conversation_id, expected_revision)
            self._idle(db, conversation_id)
            original = self._get(db, "nodes", node_id, conversation_id)
            if original["kind"] != "turn":
                raise ValueError("only turn nodes can be edited or regenerated")
            branch = self._fork(db, conversation_id, original["parent_id"], name)
            return self._append_turn(
                db,
                conversation,
                self._get(db, "branches", branch.id),
                original["user_text"] if user_text is None else user_text,
                original["mode"],
                original["input_snapshot_id"],
            )

    def get_run(self, conversation_id: str, run_id: str) -> Run:
        with self._transaction(write=False) as db:
            return _record(Run, self._get(db, "runs", run_id, conversation_id))

    def start_run(self, conversation_id: str, run_id: str) -> Run:
        with self._transaction() as db:
            run = self._get(db, "runs", run_id, conversation_id)
            if run["status"] != "queued":
                raise ConflictError("only a queued run can be started")
            db.execute(
                "UPDATE runs SET status = 'running', started_at = ? WHERE id = ?",
                (time.time(), run_id),
            )
            self._touch(db, conversation_id)
            return _record(Run, self._get(db, "runs", run_id, conversation_id))

    def finish_run(
        self,
        conversation_id: str,
        run_id: str,
        *,
        status: TerminalStatus = "succeeded",
        answer: str = "",
        error: str | None = None,
        artifacts: dict[str, Any] | None = None,
        output_snapshot_id: str | None = None,
        output_snapshot: tuple | None = None,
    ) -> Run:
        if status not in _TERMINAL:
            raise ValueError(f"invalid terminal status: {status}")
        if not isinstance(answer, str) or (error is not None and not isinstance(error, str)):
            raise ValueError("answer and error must be text")
        encoded = _json(artifacts)
        with self._transaction() as db:
            conversation = self._conversation(db, conversation_id)
            self._snapshot_ref(db, conversation, output_snapshot_id)
            run = self._get(db, "runs", run_id, conversation_id)
            if run["status"] not in {"queued", "running"}:
                raise ConflictError("a terminal run cannot be changed")
            if run["status"] == "queued" and status not in {"cancelled", "interrupted"}:
                raise ConflictError("start the run before finishing execution")
            if output_snapshot is not None:
                output_snapshot_id = self._insert_snapshot(db, conversation_id, *output_snapshot)
            db.execute(
                "UPDATE runs SET status = ?, answer = ?, error = ?, artifacts = ?, "
                "output_snapshot_id = ?, finished_at = ? WHERE id = ?",
                (status, answer, error, encoded, output_snapshot_id, time.time(), run_id),
            )
            self._touch(db, conversation_id)
            return _record(Run, self._get(db, "runs", run_id, conversation_id))

    def interrupt_pending_runs(self, conversation_id: str) -> tuple[Run, ...]:
        """Explicit crash recovery after the caller establishes exclusive ownership.

        Opening a store never interrupts another live worker. This marks unfinished
        executions; it does not replay tools or claim to restore filesystem state.
        """
        with self._transaction() as db:
            self._conversation(db, conversation_id)
            rows = db.execute(
                "SELECT * FROM runs WHERE conversation_id = ? AND status IN ('queued', 'running')",
                (conversation_id,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE runs SET status = 'interrupted', error = ?, finished_at = ? "
                    "WHERE id = ?",
                    (
                        "execution interrupted; reconcile workspace before continuing",
                        time.time(),
                        row["id"],
                    ),
                )
            if rows:
                self._touch(db, conversation_id)
            return tuple(_record(Run, self._get(db, "runs", row["id"])) for row in rows)

    def record_snapshot(
        self,
        conversation_id: str,
        storage_key: str,
        *,
        manifest: dict[str, Any] | None = None,
        as_baseline: bool = False,
    ) -> Snapshot:
        """Record an opaque S3 storage reference, never snapshot file contents here."""
        _text(storage_key, "storage_key")
        encoded = _json(manifest)
        with self._transaction() as db:
            conversation = self._conversation(db, conversation_id)
            if conversation["project_path"] is None:
                raise ValueError("snapshots require a project")
            if as_baseline and (
                conversation["base_snapshot_id"] is not None
                or db.execute(
                    "SELECT 1 FROM nodes WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()
            ):
                raise ConflictError("set the immutable baseline before the first history node")
            identity = _id("snapshot")
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                (identity, conversation_id, storage_key, encoded, time.time()),
            )
            if as_baseline:
                db.execute(
                    "UPDATE conversations SET base_snapshot_id = ? WHERE id = ?",
                    (identity, conversation_id),
                )
            self._touch(db, conversation_id)
            return _record(Snapshot, self._get(db, "snapshots", identity))

    def append_local_checkpoint(
        self,
        conversation_id: str,
        snapshot_id: str,
        *,
        label: str = "本地修改",
        expected_revision: int | None = None,
    ) -> Node:
        """Advance the active branch with saved manual changes, without a chat turn."""
        _text(label, "label")
        with self._transaction() as db:
            conversation = self._conversation(db, conversation_id, expected_revision)
            self._idle(db, conversation_id)
            self._snapshot_ref(db, conversation, snapshot_id)
            if snapshot_id is None:
                raise ValueError("local checkpoint requires a snapshot")
            branch = self._get(db, "branches", conversation["active_branch_id"], conversation_id)
            identity = _id("node")
            db.execute(
                "INSERT INTO nodes VALUES (?, ?, ?, 'local_checkpoint', ?, NULL, ?, ?, ?)",
                (
                    identity,
                    conversation_id,
                    branch["head_node_id"],
                    label,
                    self._effective_snapshot(db, conversation_id, branch["head_node_id"]),
                    snapshot_id,
                    time.time(),
                ),
            )
            db.execute(
                "UPDATE branches SET head_node_id = ? WHERE id = ?", (identity, branch["id"])
            )
            self._select(db, conversation_id, branch["id"], identity)
            return _record(Node, self._get(db, "nodes", identity))

    def _insert_snapshot(self, db, conversation_id, storage_key, manifest):
        _text(storage_key, "storage_key")
        identity = _id("snapshot")
        db.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
            (identity, conversation_id, storage_key, _json(manifest), time.time()),
        )
        return identity

    def _checkpoint(self, db, conversation_id, branch_id, snapshot_id, label):
        branch = self._get(db, "branches", branch_id, conversation_id)
        identity = _id("node")
        db.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, 'local_checkpoint', ?, NULL, ?, ?, ?)",
            (
                identity,
                conversation_id,
                branch["head_node_id"],
                label,
                self._effective_snapshot(db, conversation_id, branch["head_node_id"]),
                snapshot_id,
                time.time(),
            ),
        )
        db.execute("UPDATE branches SET head_node_id = ? WHERE id = ?", (identity, branch_id))
        self._select(db, conversation_id, branch_id, identity)
        return _record(Node, self._get(db, "nodes", identity))

    def save_checkpoint(
        self, conversation_id, key, manifest, *, label="本地修改", expected_revision=None
    ):
        """Snapshot reference and checkpoint are one history transaction."""
        _text(label, "label")
        with self._transaction() as db:
            conversation = self._conversation(db, conversation_id, expected_revision)
            self._idle(db, conversation_id)
            if conversation["base_snapshot_id"] is None:
                raise ValueError("checkpoint requires a project baseline")
            snapshot = self._insert_snapshot(db, conversation_id, key, manifest)
            return self._checkpoint(
                db, conversation_id, conversation["active_branch_id"], snapshot, label
            )

    def commit_restore(self, plan):
        """Save manual changes on the source, then move the destination cursor.

        A receipt on the immutable target snapshot reconciles a crash after the
        SQLite commit but before the external file journal's terminal update.
        """
        cid = plan["conversation_id"]
        with self._transaction() as db:
            conversation = self._conversation(db, cid, plan["revision"])
            self._idle(db, cid)
            source_branch = conversation["active_branch_id"]
            if plan["manual"]:
                snapshot = self._insert_snapshot(db, cid, plan["before_key"], plan["before"])
                self._checkpoint(db, cid, source_branch, snapshot, "本地修改")
            action = plan["action"]
            if action == "switch" and not (plan["manual"] and plan["branch_id"] == source_branch):
                branch_id = plan["branch_id"]
                branch = self._get(db, "branches", branch_id, cid)
                self._select(db, cid, branch_id, branch["head_node_id"])
            else:
                branch = self._fork(db, cid, plan["destination_node"], plan["name"])
                branch_id = branch.id
                self._select(db, cid, branch_id, plan["destination_node"])
            manifest = {**plan["target"], "restore_operation": plan["id"]}
            snapshot = self._insert_snapshot(db, cid, plan["target_key"], manifest)
            self._checkpoint(db, cid, branch_id, snapshot, "恢复检查点")
            return _record(Branch, self._get(db, "branches", branch_id, cid))
