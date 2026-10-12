"""Faults cannot split run/event or restore-receipt/journal metadata commits."""

import json
import os
import sqlite3

import pytest

from agent_runtime.state_root import native_io_path
from src.workbench.events import EventJournal
from src.workbench.restore import RestoreCoordinator
from src.workbench.schema import APPLICATION_ID, TABLES
from src.workbench.store import WorkbenchStore


def test_finish_and_event_roll_back_together(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench.sqlite3")
    chat = store.create_conversation()
    turn = store.append_turn(chat.id, "request")
    store.start_run(chat.id, turn.run.id)
    with store._transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_terminal BEFORE INSERT ON events "
            "WHEN NEW.kind='run.finished' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.finish_run(chat.id, turn.run.id, answer="answer")
    assert store.get_run(chat.id, turn.run.id).status == "running"
    assert [e["kind"] for e in EventJournal(store).read(turn.run.id)] == [
        "run.queued",
        "run.started",
    ]
    with store._transaction() as db:
        db.execute("DROP TRIGGER fail_terminal")
    store.finish_run(chat.id, turn.run.id, answer="answer")
    assert EventJournal(store).read(turn.run.id)[-1]["data"]["status"] == "succeeded"


def test_restore_receipt_and_operation_are_one_commit(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "value.txt").write_text("before")
    state = tmp_path / "state"
    store = WorkbenchStore(state / "workbench.sqlite3")
    coordinator = RestoreCoordinator(store, state, lock_dir=tmp_path / "locks")
    try:
        chat = coordinator.create("project", root)
        (root / "value.txt").write_text("manual")
        preview = coordinator.preview(chat.id, action="fork")
        tree = store.get_tree(chat.id)
        with store._transaction() as db:
            db.execute(
                "CREATE TRIGGER fail_receipt BEFORE UPDATE ON operations "
                "WHEN instr(NEW.payload,'history_committed')>0 "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            coordinator.apply(chat.id, preview["id"])
        assert store.get_tree(chat.id) == tree
        assert coordinator.pending(chat.id)
        with store._transaction() as db:
            db.execute("DROP TRIGGER fail_receipt")
        coordinator.recover(chat.id, preview["id"], decision="complete")
        assert not coordinator.pending(chat.id)
        assert coordinator._receipt(coordinator.journal.read(preview["id"]))
        assert coordinator.journal.path == store.database
    finally:
        coordinator.close()


@pytest.mark.parametrize(
    "long_path",
    [
        False,
        pytest.param(
            True, marks=pytest.mark.skipif(os.name != "nt", reason="Windows long SQLite URI")
        ),
    ],
)
def test_published_v1_journals_import_once_and_preserve_sources(tmp_path, long_path):
    if long_path:
        tmp_path = tmp_path / ("d" * (246 - len(str(tmp_path)) - 1))
        native_io_path(tmp_path).mkdir()
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(native_io_path(path)) as db:
        for statement in TABLES:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
    # Use the actual immutable v1 run rows and legacy journal contracts.
    with sqlite3.connect(native_io_path(path)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO conversations VALUES ('c','title',NULL,NULL,'b',NULL,0,1,1)")
        db.execute("INSERT INTO branches VALUES ('b','c','main',NULL,'n',1)")
        db.execute("INSERT INTO nodes VALUES ('n','c',NULL,'turn','request','chat',NULL,NULL,1)")
        db.execute(
            "INSERT INTO runs (id,conversation_id,node_id,status,answer,created_at,finished_at) "
            "VALUES ('r','c','n','succeeded','answer',1,2)"
        )
    events = tmp_path / "events.sqlite3"
    restores = tmp_path / "restores.sqlite3"
    with sqlite3.connect(native_io_path(events)) as db:
        db.execute(
            "CREATE TABLE events (run_id TEXT,sequence INTEGER,kind TEXT,payload TEXT,created_at REAL)"
        )
        db.execute("PRAGMA application_id=1179403606")
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO events VALUES ('r',1,'answer.delta',?,1)",
            (json.dumps({"text": "answer"}),),
        )
    with sqlite3.connect(native_io_path(restores)) as db:
        db.execute("CREATE TABLE operations (id TEXT PRIMARY KEY,root TEXT,payload TEXT)")
        db.execute(
            "INSERT INTO operations VALUES ('o','root',?)",
            (json.dumps({"id": "o", "root": "root", "status": "needs_recovery"}),),
        )
    saved_events, saved_restores = (
        native_io_path(events).read_bytes(),
        native_io_path(restores).read_bytes(),
    )
    store = WorkbenchStore(path)
    assert store.get_run("c", "r").answer == "answer"
    assert EventJournal(store).partial_answer("r") == "answer"
    with store._transaction(write=False) as db:
        assert json.loads(db.execute("SELECT payload FROM operations").fetchone()[0])["id"] == "o"
    assert (
        native_io_path(events).read_bytes() == saved_events
        and native_io_path(restores).read_bytes() == saved_restores
    )
    assert len(EventJournal(WorkbenchStore(path)).read("r")) == 2
    assert EventJournal(store).read("r")[-1]["kind"] == "run.finished"
