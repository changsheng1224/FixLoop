"""Transactional append-only facts. Checkpoints are sealed references, not truth."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from .models import Plan, digest


class PlanStore:
    def __init__(self, root: Path, identity: dict):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.identity = dict(identity)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(root / "journal.sqlite3", timeout=5, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events "
            "(seq INTEGER PRIMARY KEY, kind TEXT, payload TEXT, previous TEXT, checksum TEXT)"
        )
        self.db.commit()
        try:
            history = self.events()
            if history:
                if history[0]["kind"] != "identity" or history[0]["payload"] != identity:
                    raise ValueError("resume_identity_mismatch")
            else:
                self.append("identity", identity)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def append(self, kind: str, payload: dict) -> int:
        with self._lock, self.db:
            last = self.db.execute("SELECT seq, checksum FROM events ORDER BY seq DESC LIMIT 1")
            row = last.fetchone()
            seq, previous = (row[0] + 1, row[1]) if row else (1, "")
            checksum = digest({"seq": seq, "kind": kind, "payload": payload, "previous": previous})
            self.db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (
                    seq,
                    kind,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                    previous,
                    checksum,
                ),
            )
        return seq

    def events(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        result, previous = [], ""
        for seq, kind, encoded, parent, checksum in rows:
            payload = json.loads(encoded)
            event = {"seq": seq, "kind": kind, "payload": payload, "previous": parent}
            if seq != len(result) + 1 or parent != previous or digest(event) != checksum:
                raise ValueError("journal_integrity_invalid")
            result.append({**event, "checksum": checksum})
            previous = checksum
        return result

    def save_plan(self, plan: Plan) -> None:
        if not plan.verify():
            raise ValueError("plan_checksum_invalid")
        self.append("plan", plan.to_dict())

    def load_plan(self) -> Plan | None:
        for event in reversed(self.events()):
            if event["kind"] == "plan":
                plan = Plan.from_dict(event["payload"])
                if not plan.verify():
                    raise ValueError("plan_checksum_invalid")
                return plan
        return None

    def latest(self, kind: str, key: str) -> dict[str, dict]:
        result = {}
        for event in self.events():
            if event["kind"] == kind:
                value = event["payload"]
                result[value[key]] = value
        return result

    def validate_execution_history(self):
        attempts, operations = {}, {}
        phases = {"prepared": 0, "dispatched": 1, "result_recorded": 2, "reconciled": 3}
        for event in self.events():
            kind, value = event["kind"], event["payload"]
            if kind not in {"attempt", "operation"}:
                continue
            mapping = attempts if kind == "attempt" else operations
            key = value["attempt_id" if kind == "attempt" else "operation_id"]
            previous = mapping.get(key)
            phase = value.get("phase")
            if phase not in phases or (previous is None and phase != "prepared"):
                raise ValueError("journal_execution_phase_invalid")
            if previous:
                if phases[phase] < phases[previous["phase"]]:
                    raise ValueError("journal_execution_phase_invalid")
                immutable = (
                    (
                        "attempt_id",
                        "plan_id",
                        "plan_version",
                        "node_id",
                        "allowed_tools",
                        "idempotency_key",
                        "workspace_before",
                        "owner",
                    )
                    if kind == "attempt"
                    else (
                        "operation_id",
                        "attempt_id",
                        "node_id",
                        "plan_version",
                        "tool",
                        "call_id",
                        "args_hash",
                        "effect",
                        "workspace_before",
                    )
                )
                if any(value.get(k) != previous.get(k) for k in immutable):
                    raise ValueError("journal_execution_identity_invalid")
            if kind == "operation":
                attempt = attempts.get(value["attempt_id"])
                if (
                    not attempt
                    or value["node_id"] != attempt["node_id"]
                    or value["plan_version"] != attempt["plan_version"]
                ):
                    raise ValueError("journal_operation_attempt_mismatch")
                if value.get("receipt"):
                    receipt = value["receipt"]
                    if (
                        receipt.get("call_id") != value["call_id"]
                        or receipt.get("tool") != value["tool"]
                        or receipt.get("run_id") != self.identity["run_id"]
                    ):
                        raise ValueError("journal_receipt_identity_invalid")
                    body = {k: v for k, v in receipt.items() if k != "receipt_id"}
                    encoded = json.dumps(
                        body, sort_keys=True, ensure_ascii=False, default=str
                    ).encode()
                    if (
                        receipt.get("receipt_id")
                        != "receipt-" + hashlib.sha256(encoded).hexdigest()[:20]
                    ):
                        raise ValueError("journal_receipt_checksum_invalid")
            mapping[key] = value

    def checkpoint(
        self,
        plan: Plan | None,
        long_task_state: dict | None = None,
        coordination_seal: dict | None = None,
    ) -> dict:
        with self._lock:
            return self._checkpoint(plan, long_task_state, coordination_seal)

    def _checkpoint(
        self,
        plan: Plan | None,
        long_task_state: dict | None = None,
        coordination_seal: dict | None = None,
    ) -> dict:
        events = self.events()
        attempts = self.latest("attempt", "attempt_id")
        payload = {
            "schema_version": "2" if coordination_seal else "1",
            "identity": self.identity,
            "plan_checksum": plan.plan_checksum if plan else "",
            "plan_version": plan.plan_version if plan else 0,
            "state_revision": plan.state_revision if plan else 0,
            "journal_sequence": events[-1]["seq"],
            "journal_checksum": events[-1]["checksum"],
            "active_attempts": sorted(k for k, v in attempts.items() if v["phase"] != "reconciled"),
            "long_task_state": dict(long_task_state or {}),
        }
        if coordination_seal is not None:
            payload["coordination_seal"] = dict(coordination_seal)
        seal = {**payload, "checksum": digest(payload)}
        self.append("checkpoint", seal)
        return seal

    def put_blob(self, text: str) -> str:
        with self._lock:
            return self._put_blob(text)

    def _put_blob(self, text: str) -> str:
        raw = text.encode("utf-8")
        key = hashlib.sha256(raw).hexdigest()
        directory = self.root / "blobs"
        directory.mkdir(exist_ok=True)
        target = directory / key
        if not target.exists():
            temporary = directory / ("." + uuid.uuid4().hex)
            with temporary.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        return key

    def get_blob(self, key: str) -> str:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("blob_reference_invalid")
        raw = (self.root / "blobs" / key).read_bytes()
        if hashlib.sha256(raw).hexdigest() != key:
            raise ValueError("blob_checksum_invalid")
        return raw.decode("utf-8")

    def verify_checkpoint(self, checkpoint: dict, coordination_seal: dict | None = None) -> None:
        payload = {k: v for k, v in checkpoint.items() if k != "checksum"}
        if (
            checkpoint.get("checksum") != digest(payload)
            or payload.get("identity") != self.identity
        ):
            raise ValueError("checkpoint_identity_or_checksum_invalid")
        if coordination_seal is not None:
            saved = payload.get("coordination_seal")
            if not saved or saved != coordination_seal:
                raise ValueError("checkpoint_coordination_mismatch")
        events = self.events()
        sequence = payload.get("journal_sequence", 0)
        if not isinstance(sequence, int) or not 1 <= sequence <= len(events):
            raise ValueError("checkpoint_journal_mismatch")
        if events[sequence - 1]["checksum"] != payload.get("journal_checksum"):
            raise ValueError("checkpoint_journal_mismatch")
        prefix = events[:sequence]
        plans = [e["payload"] for e in prefix if e["kind"] == "plan"]
        saved = plans[-1] if plans else {}
        for key, source in (
            ("plan_checksum", "plan_checksum"),
            ("plan_version", "plan_version"),
            ("state_revision", "state_revision"),
        ):
            if payload[key] != saved.get(source, "" if key == "plan_checksum" else 0):
                raise ValueError("checkpoint_plan_mismatch")
        active = {}
        for event in prefix:
            if event["kind"] == "attempt":
                a = event["payload"]
                active[a["attempt_id"]] = a["phase"]
        if payload["active_attempts"] != sorted(k for k, v in active.items() if v != "reconciled"):
            raise ValueError("checkpoint_attempt_mismatch")
