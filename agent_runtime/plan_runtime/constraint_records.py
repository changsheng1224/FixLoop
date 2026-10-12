"""Append-only explicit constraints; the active strings are a derived projection."""

from copy import deepcopy

from .models import digest, new_id


def constraint_history(records):
    history, latest = [], {}
    for raw in records:
        record = deepcopy(raw)
        checksum = record.pop("checksum", "")
        if checksum != digest(record):
            raise ValueError("constraint_checksum_invalid")
        record["checksum"] = checksum
        key = record.get("constraint_id")
        if not isinstance(key, str) or not key:
            raise ValueError("constraint_chain_invalid")
        previous = latest.get(key)
        if (
            type(record.get("revision")) is not int
            or record["revision"] != (previous["revision"] + 1 if previous else 1)
            or record.get("status") not in {"active", "revoked"}
            or (record["status"] == "revoked" and previous is None)
            or (previous and previous["status"] == "revoked")
            or not isinstance(record.get("statement"), str)
            or not record["statement"].strip()
            or record.get("source_kind") not in {"user", "runtime"}
            or not isinstance(record.get("source_refs"), list)
            or not record["source_refs"]
            or record.get("supersedes_ref")
            != ({"constraint_id": key, "revision": previous["revision"]} if previous else None)
        ):
            raise ValueError("constraint_chain_invalid")
        if previous:
            previous["status"] = "superseded"
        history.append(record)
        latest[key] = record
    return history, latest


def active_constraints(records):
    return [
        r["statement"] for r in constraint_history(records)[1].values() if r["status"] == "active"
    ]


def new_constraint(
    records,
    *,
    action,
    source_refs,
    source_kind="user",
    statement=None,
    constraint_id=None,
    expected_revision=None,
):
    _, latest = constraint_history(records)
    previous = latest.get(constraint_id) if isinstance(constraint_id, str) else None
    if action not in {"add", "replace", "revoke"}:
        raise ValueError("constraint_action_invalid")
    if action == "add":
        if constraint_id is not None or expected_revision is not None:
            raise ValueError("constraint_revision_conflict")
    elif (
        not previous
        or previous["status"] != "active"
        or type(expected_revision) is not int
        or previous["revision"] != expected_revision
    ):
        raise ValueError("constraint_revision_conflict")
    key = constraint_id or new_id("constraint")
    record = {
        "constraint_id": key,
        "revision": previous["revision"] + 1 if previous else 1,
        "status": "revoked" if action == "revoke" else "active",
        "statement": previous["statement"] if action == "revoke" else statement,
        "source_kind": source_kind,
        "source_refs": deepcopy(source_refs),
        "supersedes_ref": {"constraint_id": key, "revision": previous["revision"]}
        if previous
        else None,
    }
    record["checksum"] = digest(record)
    constraint_history([*records, record])
    return record
