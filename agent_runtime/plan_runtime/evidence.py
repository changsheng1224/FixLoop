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
        return self.inspect(key, historical=historical, seen=seen)["status"] == "valid"

    def inspect(self, key: str, *, historical: bool = False, seen: set | None = None) -> dict:
        """Explain the existing predicate; historical checks never assert current versions."""
        result = {
            "evidence_ref": key,
            "kind": "",
            "status": "invalid",
            "reason": "",
            "use": "historical" if historical else "current",
            "dependencies": [],
        }

        def finish(status, reason):
            return {**result, "status": status, "reason": reason}

        seen = set(seen or ())
        if key in seen:
            return finish("invalid", "evidence_cycle")
        seen.add(key)
        record = self.store.latest("evidence", "evidence_id").get(key)
        if not record:
            return finish("invalid", "evidence_missing")
        if record.get("checksum") != digest({k: v for k, v in record.items() if k != "checksum"}):
            return finish("invalid", "evidence_checksum_mismatch")
        if record.get("identity") != self.store.identity:
            return finish("invalid", "evidence_scope_mismatch")
        result["kind"] = record.get("kind", "")
        result["record_checksum"] = record["checksum"]
        if record.get("status") != "success":
            return finish("invalid", "evidence_not_successful")
        if not historical:
            versions = record.get("file_versions")
            if not isinstance(versions, dict):
                return finish("unknown", "file_versions_missing")
            try:
                current = snapshot(self.workspace)
            except (OSError, ValueError):
                return finish("unknown", "workspace_unverifiable")
            if (record.get("whole_workspace") and versions != current) or any(
                current.get(p) != h for p, h in versions.items()
            ):
                return finish("stale", "source_changed")
        kind = record["kind"]
        if kind == "observation_present":
            result["observation_id"] = record.get("observation_id", "")
            archived = record.get("observation_record")
            if archived and record.get("blob_ref"):
                try:
                    text = self.store.get_blob(record["blob_ref"])
                except (ValueError, OSError):
                    return finish("invalid", "blob_unavailable")
                expected = record.get("observation_checksum", "")
                identity_ok = (
                    archived.get("observation_id") == record.get("observation_id")
                    and archived.get("run_id") == self.store.identity["run_id"]
                    and archived.get("session_id") == self.store.identity["session_id"]
                    and archived.get("checksum") == expected
                )
            else:
                state = {
                    **self.observation_state,
                    "id": self.store.identity["session_id"],
                    "session_scope": {"session_id": self.store.identity["session_id"]},
                }
                observations = ObservationStore(state, self.workspace, self.state_root)
                try:
                    observation = observations.get(record.get("observation_id", ""))
                    if observation is None:
                        return finish("invalid", "observation_missing")
                    identity_ok = (
                        observation.run_id == self.store.identity["run_id"]
                        and observation.session_id == self.store.identity["session_id"]
                    )
                    text = observations.expand(observation.observation_id)
                    expected = observation.checksum
                    identity_ok = identity_ok and expected == record.get("observation_checksum")
                finally:
                    observations.close()
            if not identity_ok:
                return finish("invalid", "observation_scope_or_checksum_mismatch")
            if (
                not expected
                or hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() != expected
            ):
                return finish("invalid", "blob_checksum_mismatch")
        elif kind == "analysis_recorded":
            if not record.get("conclusion") or not record.get("input_refs"):
                return finish("invalid", "analysis_inputs_or_conclusion_missing")
            result["dependencies"] = [
                self.inspect(ref, historical=historical, seen=seen) for ref in record["input_refs"]
            ]
        elif kind == "patch_applied":
            receipt = record.get("receipt", {})
            if not (
                receipt.get("status") == "success"
                and record.get("changed_paths")
                and receipt.get("run_id") == self.store.identity["run_id"]
                and receipt.get("call_id")
                and receipt.get("receipt_id")
                and record.get("execution_stopped") is True
                and record.get("observation_refs")
            ):
                return finish("invalid", "patch_receipt_invalid")
            result["dependencies"] = [
                self.inspect(ref, historical=True, seen=seen) for ref in record["observation_refs"]
            ]
        elif kind == "tests_passed":
            receipt = record.get("receipt", {})
            if not (
                receipt.get("command")
                and receipt.get("completed") is True
                and receipt.get("all_passed") is True
                and receipt.get("total_tests", 0) > 0
                and receipt.get("run_id") == self.store.identity["run_id"]
                and receipt.get("attempt_id") == record["attempt_id"]
                and record.get("execution_stopped") is True
            ):
                return finish("invalid", "verification_receipt_invalid")
        else:
            return finish("invalid", "evidence_kind_unsupported")
        failed = next((item for item in result["dependencies"] if item["status"] != "valid"), None)
        if failed:
            result["failed_dependency"] = failed["evidence_ref"]
            return finish(failed["status"], "dependency_unusable:" + failed["reason"])
        return finish(
            "valid", "historical_integrity_checked" if historical else "current_predicate_checked"
        )

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
