"""Explicit decision revisions and read-only, node-scoped evidence checks."""

from copy import deepcopy

from .models import digest, new_id


def decision_history(records):
    """Validate append-only chains; legacy notes are audit facts, not decisions."""
    history, latest = [], {}
    for raw in records:
        if raw.get("record_type") != "decision":
            continue
        record = deepcopy(raw)
        checksum = record.pop("checksum", "")
        if checksum != digest(record):
            raise ValueError("decision_checksum_invalid")
        record["checksum"] = checksum
        key = record.get("decision_id")
        if not isinstance(key, str) or not key:
            raise ValueError("decision_chain_invalid")
        previous = latest.get(key)
        expected = {"decision_id": key, "revision": previous["revision"]} if previous else None
        if (
            type(record.get("revision")) is not int
            or record["revision"] != (previous["revision"] + 1 if previous else 1)
            or record.get("supersedes_ref") != expected
            or record.get("status") != "active"
            or not all(
                isinstance(record.get(name), str) and record[name].strip()
                for name in ("statement", "source", "node_id", "plan_id")
            )
            or not isinstance(record.get("rationale"), str)
            or type(record.get("plan_version")) is not int
            or record["plan_version"] < 1
            or not isinstance(record.get("evidence_refs"), list)
            or not record["evidence_refs"]
            or any(not isinstance(ref, str) or not ref for ref in record["evidence_refs"])
            or not isinstance(record.get("evidence_checksums"), dict)
            or set(record["evidence_checksums"]) != set(record["evidence_refs"])
            or any(
                not isinstance(value, str) or len(value) != 64
                for value in record["evidence_checksums"].values()
            )
        ):
            raise ValueError("decision_chain_invalid")
        if previous:
            if (
                previous["node_id"] != record["node_id"]
                or previous["plan_id"] != record["plan_id"]
                or previous["plan_version"] > record["plan_version"]
            ):
                raise ValueError("decision_chain_scope_invalid")
            previous["status"] = "superseded"
        history.append(record)
        latest[key] = record
    return history, latest


def new_decision(
    records,
    plan,
    evidence,
    statement,
    *,
    rationale,
    source,
    evidence_refs,
    node_id,
    decision_id=None,
    expected_revision=None,
):
    _, latest = decision_history(records)
    if any(record["plan_id"] != plan.plan_id for record in latest.values()):
        raise ValueError("decision_plan_identity_mismatch")
    if decision_id is not None and (
        not isinstance(decision_id, str)
        or not decision_id
        or type(expected_revision) is not int
        or expected_revision < 1
    ):
        raise ValueError("decision_revision_conflict")
    if decision_id is None and expected_revision is not None:
        raise ValueError("decision_revision_conflict")
    previous = latest.get(decision_id) if decision_id else None
    if decision_id and (not previous or previous["revision"] != expected_revision):
        raise ValueError("decision_revision_conflict")
    if previous and previous["node_id"] != node_id:
        raise ValueError("decision_scope_change_requires_new_id")
    if (
        not isinstance(statement, str)
        or not statement.strip()
        or not isinstance(source, str)
        or not source.strip()
        or not isinstance(rationale, str)
        or not isinstance(evidence_refs, list | tuple)
        or not evidence_refs
        or any(not isinstance(ref, str) or not ref for ref in evidence_refs)
    ):
        raise ValueError("decision_content_or_source_missing")
    refs = list(dict.fromkeys(evidence_refs))
    inputs = {ref: evidence.inspect(ref) for ref in refs}
    if not all(check["status"] == "valid" for check in inputs.values()):
        raise ValueError("decision_evidence_unusable")
    key = decision_id or new_id("decision")
    record = {
        "record_type": "decision",
        "decision_id": key,
        "revision": previous["revision"] + 1 if previous else 1,
        "status": "active",
        "statement": statement,
        "rationale": rationale,
        "source": source,
        "evidence_refs": refs,
        "evidence_checksums": {ref: check["record_checksum"] for ref, check in inputs.items()},
        "node_id": node_id,
        "plan_id": plan.plan_id,
        "plan_version": plan.plan_version,
        "supersedes_ref": {"decision_id": key, "revision": previous["revision"]}
        if previous
        else None,
    }
    return {**record, "checksum": digest(record)}


def project_decisions(records, plan, evidence, node_id):
    """Derive needs_review without changing state or reactivating old versions."""
    _, latest = decision_history(records)
    active, checks = [], []
    for record in latest.values():
        if record["plan_id"] != plan.plan_id:
            raise ValueError("decision_plan_identity_mismatch")
        if record["node_id"] != node_id:
            continue
        inputs = [evidence.inspect(ref) for ref in record["evidence_refs"]]
        failed = next((item for item in inputs if item["status"] != "valid"), None)
        changed = any(
            item.get("record_checksum") != record["evidence_checksums"][item["evidence_ref"]]
            for item in inputs
        )
        reason = (
            "plan_version_changed"
            if record["plan_version"] != plan.plan_version
            else (
                "evidence_unusable:" + failed["reason"]
                if failed
                else ("evidence_record_changed" if changed else "current_inputs_checked")
            )
        )
        status = "active" if reason == "current_inputs_checked" else "needs_review"
        checks.append(
            {
                "decision_id": record["decision_id"],
                "revision": record["revision"],
                "checksum": record["checksum"],
                "status": status,
                "reason": reason,
                "node_id": node_id,
                "evidence_checks": inputs,
                "expected_evidence_checksums": deepcopy(record["evidence_checksums"]),
            }
        )
        if status == "active":
            active.append(
                {
                    key: deepcopy(record[key])
                    for key in (
                        "decision_id",
                        "revision",
                        "statement",
                        "rationale",
                        "source",
                        "evidence_refs",
                    )
                }
            )
    return {"active": active, "checks": checks}
