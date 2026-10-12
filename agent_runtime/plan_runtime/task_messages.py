"""Immutable messages registered by trusted host APIs, never by model tools."""

from copy import deepcopy

from .models import digest


class TaskMessages:
    def __init__(self, store):
        self.store = store

    def records(self):
        result = {}
        for event in self.store.events():
            if event["kind"] != "task_message":
                continue
            record = event["payload"]
            body = {k: v for k, v in record.items() if k != "checksum"}
            if (
                record.get("checksum") != digest(body)
                or record.get("identity") != self.store.identity
                or record.get("source_kind") not in {"user", "model", "runtime"}
                or not isinstance(record.get("body"), str)
                or record.get("body_checksum") != digest(record["body"])
                or not isinstance(record.get("message_id"), str)
                or not record["message_id"]
            ):
                raise ValueError("task_message_integrity_invalid")
            key = record["message_id"]
            if key in result and result[key] != record:
                raise ValueError("task_message_identity_conflict")
            result[key] = record
        return result

    def register(self, message_id, body, *, source_kind):
        if not isinstance(message_id, str) or not message_id or not isinstance(body, str):
            raise ValueError("task_message_invalid")
        if source_kind not in {"user", "model", "runtime"}:
            raise ValueError("task_message_source_invalid")
        record = {
            "message_id": message_id,
            "identity": dict(self.store.identity),
            "source_kind": source_kind,
            "body": body,
            "body_checksum": digest(body),
        }
        record["checksum"] = digest(record)
        previous = self.records().get(message_id)
        if previous is not None:
            if previous != record:
                raise ValueError("task_message_identity_conflict")
        else:
            self.store.append("task_message", record)
        return deepcopy(record)

    def reference(self, message_id, start=0, end=None):
        record = self.records().get(message_id)
        if record is None:
            raise ValueError("source_message_missing")
        end = len(record["body"]) if end is None else end
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(record["body"])
        ):
            raise ValueError("source_quote_mismatch")
        ref = {
            "message_id": message_id,
            "message_checksum": record["checksum"],
            "start": start,
            "end": end,
            "quote": record["body"][start:end],
        }
        self.validate([ref], allowed={record["source_kind"]})
        return ref

    def validate(self, refs, *, allowed):
        if not isinstance(refs, list) or not refs:
            raise ValueError("source_refs_missing")
        messages = self.records()
        kinds = set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {
                "message_id",
                "message_checksum",
                "start",
                "end",
                "quote",
            }:
                raise ValueError("source_ref_invalid")
            if not isinstance(ref["message_id"], str) or not isinstance(
                ref["message_checksum"], str
            ):
                raise ValueError("source_ref_invalid")
            record = messages.get(ref["message_id"])
            if record is None or record["checksum"] != ref["message_checksum"]:
                raise ValueError("source_message_mismatch")
            if record["source_kind"] not in allowed:
                raise ValueError("source_role_not_allowed")
            start, end = ref["start"], ref["end"]
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(record["body"])
                or ref["quote"] != record["body"][start:end]
            ):
                raise ValueError("source_quote_mismatch")
            kinds.add(record["source_kind"])
        return kinds
