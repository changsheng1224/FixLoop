"""Durable, idempotent terminal commits owned by one workbench service."""

import json
import threading
from pathlib import Path

from agent_runtime.atomic_io import atomic_write_text

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class TerminalRecovery:
    def __init__(self, store, directory: Path):
        self.store = store
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pending = {}
        for path in self.directory.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if path.name != payload["run_id"] + ".json":
                raise ValueError("invalid pending terminal identity")
            self.pending[payload["run_id"]] = payload

    def prepare(self, conversation_id, run_id, **result):
        payload = {"conversation_id": conversation_id, "run_id": run_id, "result": result}
        with self.lock:
            self.pending[run_id] = payload
            atomic_write_text(self.directory / (run_id + ".json"), json.dumps(payload))

    def retry(self, run_id):
        with self.lock:
            payload = self.pending.get(run_id)
            if payload is None:
                return None
            cid = payload["conversation_id"]
            run = self.store.get_run(cid, run_id)
            if run.status not in TERMINAL:
                result = dict(payload["result"])
                resource = self.store.resource(run_id)
                if resource is not None and resource.get("state") != "stopped":
                    result.update(
                        status="interrupted",
                        error="execution_cleanup_unconfirmed",
                        output_snapshot=None,
                        candidate=None,
                    )
                    result["artifacts"] = {
                        **(result.get("artifacts") or {}),
                        "recovery_required": True,
                    }
                run = self.store.finish_run(cid, run_id, **result)
            # A crash after the DB commit can only remove this journal, never
            # insert another snapshot, candidate, or run.finished event.
            (self.directory / (run_id + ".json")).unlink(missing_ok=True)
            self.pending.pop(run_id, None)
            return run

    def for_conversation(self, cid):
        with self.lock:
            return [rid for rid, p in self.pending.items() if p["conversation_id"] == cid]

    def flush(self):
        with self.lock:
            for run_id, payload in self.pending.items():
                atomic_write_text(self.directory / (run_id + ".json"), json.dumps(payload))
