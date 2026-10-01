"""Bounded graph replacement at quiescent points, preserving write history."""

from __future__ import annotations

from dataclasses import replace

from .reducer import refresh
from .validate import MAX_REPLANS, validate_plan
from .workspace import snapshot


def check_replan(session):
    """Cheap preflight before generation; submission repeats these gates."""
    session._fence()
    old = session.plan
    if old.plan_version > MAX_REPLANS:
        raise ValueError("replan_budget_exceeded")
    if any(n.status in {"running", "uncertain"} for n in old.nodes):
        raise ValueError("replan_requires_quiescence")
    if any(
        a["phase"] != "reconciled" for a in session.store.latest("attempt", "attempt_id").values()
    ):
        raise ValueError("replan_active_attempt")
    for previous in old.nodes:
        if previous.kind == "edit" and previous.status == "succeeded":
            rollbacks = [
                e["payload"]
                for e in session.store.events()
                if e["kind"] == "rollback" and e["payload"].get("plan_version") == old.plan_version
            ]
            latest = rollbacks[-1] if rollbacks else {}
            if not (
                latest.get("completed") is True
                and latest.get("workspace_after") == latest.get("expected_after")
                and latest.get("workspace_after") == snapshot(session.workspace)
            ):
                raise ValueError("replan_rollback_unconfirmed")


def replan(session, candidate, *, reason: str, evidence_refs: list[str]):
    session._fence()
    if reason.startswith("verification_failed:"):
        check_replan(session)
    old = session.plan
    # Generic callers may retain completed writes. Their original submission
    # checks below still permit that; fresh retry graphs require rollback.
    if old.plan_version > MAX_REPLANS:
        raise ValueError("replan_budget_exceeded")
    if any(n.status in {"running", "uncertain"} for n in old.nodes):
        raise ValueError("replan_requires_quiescence")
    if not reason or not evidence_refs or not all(session.evidence.get(r) for r in evidence_refs):
        raise ValueError("replan_trigger_evidence_required")
    if any(
        getattr(candidate, key) != getattr(old, key)
        for key in (
            "plan_id",
            "task_id",
            "run_id",
            "workspace_id",
            "session_id",
        )
    ):
        raise ValueError("replan_identity_mismatch")
    active = session.store.latest("attempt", "attempt_id")
    if any(a["phase"] != "reconciled" for a in active.values()):
        raise ValueError("replan_active_attempt")
    new_nodes = {n.node_id: n for n in candidate.nodes}
    for previous in old.nodes:
        if (
            previous.node_id in new_nodes
            and new_nodes[previous.node_id].definition() != previous.definition()
        ):
            raise ValueError("stable_node_id_semantics_changed")
        if previous.kind == "edit" and previous.status == "succeeded":
            rolled_back = any(
                e["kind"] == "rollback"
                and e["payload"].get("completed") is True
                and e["payload"].get("plan_version") == old.plan_version
                and e["payload"].get("workspace_after") == e["payload"].get("expected_after")
                and e["payload"].get("workspace_after") == snapshot(session.workspace)
                for e in session.store.events()
            )
            if rolled_back and previous.node_id not in new_nodes:
                continue
            if previous.node_id not in new_nodes or not all(
                session.evidence.valid(ref)
                for ref in previous.output_evidence_refs
                if (session.evidence.get(ref) or {}).get("kind") == "patch_applied"
            ):
                raise ValueError("completed_write_history_required")
    nodes = []
    for node in candidate.nodes:
        previous = next((n for n in old.nodes if n.node_id == node.node_id), None)
        if (
            previous
            and previous.status == "succeeded"
            and all(session.evidence.valid(ref) for ref in previous.output_evidence_refs)
        ):
            nodes.append(previous)
        elif previous and previous.kind == "edit" and previous.status == "succeeded":
            nodes.append(previous)
        else:
            nodes.append(
                replace(
                    node,
                    status="pending",
                    attempt_id="",
                    failure="",
                    output_evidence_refs=(),
                    receipt_refs=(),
                    started_at=0,
                    ended_at=0,
                )
            )
    candidate = replace(
        candidate,
        nodes=tuple(nodes),
        plan_version=old.plan_version + 1,
        state_revision=old.state_revision + 1,
        parent_plan_checksum=old.plan_checksum,
        replan_reason=reason,
        replan_evidence_refs=tuple(evidence_refs),
        status="active",
    ).seal()
    validate_plan(candidate, session.registry, identity=session.identity)
    candidate = refresh(candidate, session.evidence)
    session.commit(candidate)
    session.emit(
        "replan_committed",
        reason=reason,
        trigger_ref=reason.removeprefix("verification_failed:")
        if reason.startswith("verification_failed:")
        else "",
        evidence_refs=evidence_refs,
        parent_plan_checksum=old.plan_checksum,
    )
    session.checkpoint()
    return candidate
