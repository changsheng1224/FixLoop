"""Executable all-of completion predicates over scoped durable facts."""

from __future__ import annotations

import hashlib

from agent_runtime.context_runtime import ObservationStore

from .models import PlanNode, digest, new_id
from .workspace import snapshot


class EvidenceLedger:
    def __init__(self, store, workspace: str, observation_state: dict, state_root: str = ""):
        self.store = store
        self.workspace = workspace
        self.observation_state = observation_state
        self.state_root = state_root

    def add(self, kind: str, *, attempt_id: str, **fields) -> str:
        record = {
            "evidence_id": new_id("E"),
            "kind": kind,
            "identity": self.store.identity,
            "attempt_id": attempt_id,
            **fields,
        }
        record["checksum"] = digest(record)
        self.store.append("evidence", record)
        return record["evidence_id"]

    def get(self, key: str) -> dict | None:
        record = self.store.latest("evidence", "evidence_id").get(key)
        if not record:
            return None
        raw = {k: v for k, v in record.items() if k != "checksum"}
        if record.get("checksum") != digest(raw) or record.get("identity") != self.store.identity:
            return None
        return record

    def valid(self, key: str, *, historical: bool = False, seen: set | None = None) -> bool:
        seen = set(seen or ())
        if key in seen:
            return False
        seen.add(key)
        record = self.get(key)
        if not record or record.get("status") != "success":
            return False
        if not historical:
            versions = record.get("file_versions")
            if versions is None:
                return False
            current = snapshot(self.workspace)
            if record.get("whole_workspace"):
                if versions != current:
                    return False
            elif any(current.get(p) != h for p, h in versions.items()):
                return False
        if record["kind"] == "observation_present":
            archived = record.get("observation_record")
            if archived and record.get("blob_ref"):
                try:
                    text = self.store.get_blob(record["blob_ref"])
                except (ValueError, OSError):
                    return False
                return (
                    archived.get("observation_id") == record["observation_id"]
                    and archived.get("run_id") == self.store.identity["run_id"]
                    and archived.get("session_id") == self.store.identity["session_id"]
                    and archived.get("checksum") == record["observation_checksum"]
                    and hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
                    == record["observation_checksum"]
                )
            state = {
                **self.observation_state,
                "id": self.store.identity["session_id"],
                "session_scope": {"session_id": self.store.identity["session_id"]},
            }
            observations = ObservationStore(state, self.workspace, self.state_root)
            try:
                observation = observations.get(record["observation_id"])
                if observation is None or observation.run_id != self.store.identity["run_id"]:
                    return False
                if observation.session_id != self.store.identity["session_id"]:
                    return False
                text = observations.expand(observation.observation_id)
                return (
                    bool(observation.checksum)
                    and observation.checksum == record["observation_checksum"]
                    and hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
                    == observation.checksum
                )
            finally:
                observations.close()
        if record["kind"] == "analysis_recorded":
            return bool(record.get("conclusion") and record.get("input_refs")) and all(
                self.valid(ref, historical=historical, seen=seen) for ref in record["input_refs"]
            )
        if record["kind"] == "patch_applied":
            receipt = record.get("receipt", {})
            return (
                receipt.get("status") == "success"
                and bool(record.get("changed_paths"))
                and receipt.get("run_id") == self.store.identity["run_id"]
                and bool(receipt.get("call_id"))
                and bool(receipt.get("receipt_id"))
                and record.get("execution_stopped") is True
                and all(
                    self.valid(r, historical=True, seen=seen)
                    for r in record.get("observation_refs", ())
                )
                and bool(record.get("observation_refs"))
            )
        if record["kind"] == "tests_passed":
            receipt = record.get("receipt", {})
            return (
                bool(receipt.get("command"))
                and receipt.get("completed") is True
                and receipt.get("all_passed") is True
                and receipt.get("total_tests", 0) > 0
                and receipt.get("run_id") == self.store.identity["run_id"]
                and receipt.get("attempt_id") == record["attempt_id"]
                and record.get("execution_stopped") is True
            )
        return False

    def completion(self, node: PlanNode, refs: tuple[str, ...], attempt_id: str) -> bool:
        for predicate in node.completion:
            selected = predicate.evidence_refs or refs
            matching = [r for r in selected if (self.get(r) or {}).get("kind") == predicate.kind]
            if not matching or not all(
                self.valid(r)
                and (
                    predicate.kind == "observation_present"
                    or (self.get(r) or {}).get("attempt_id") == attempt_id
                )
                for r in matching
            ):
                return False
        return True
