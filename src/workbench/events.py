"""SSE replay is a projection of the authoritative workbench database."""

import json


class EventJournal:
    def __init__(self, store):
        self.store = store
        self.path = store.database

    def publish(self, run_id, kind, payload):
        if kind in {"run.queued", "run.started", "run.finished"}:
            raise ValueError("lifecycle events must commit with the corresponding run transition")
        with self.store._transaction() as db:
            return self.store._event(db, run_id, kind, payload)

    def read(self, run_id, after=0, *, limit=256):
        with self.store._transaction(write=False) as db:
            return tuple(
                {"sequence": row[0], "run_id": run_id, "kind": row[1], "data": json.loads(row[2])}
                for row in db.execute(
                    "SELECT sequence,kind,payload FROM events WHERE run_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (run_id, after, limit),
                )
            )

    def latest(self, run_id):
        with self.store._transaction(write=False) as db:
            return db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]

    def partial_answer(self, run_id):
        with self.store._transaction(write=False) as db:
            return "".join(
                json.loads(row[0])["text"]
                for row in db.execute(
                    "SELECT payload FROM events WHERE run_id=? AND kind='answer.delta' "
                    "ORDER BY sequence",
                    (run_id,),
                )
            )
