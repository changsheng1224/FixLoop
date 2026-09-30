"""The only node state transition API; every revision is a new sealed value."""

from __future__ import annotations

import time
from dataclasses import replace

from .models import Plan

TRANSITIONS = {
    "pending": {"ready", "blocked", "stale", "cancelled"},
    "ready": {"running", "blocked", "stale", "cancelled"},
    "running": {"succeeded", "failed", "cancelled", "uncertain"},
    "uncertain": {"succeeded", "failed", "blocked"},
    "succeeded": {"stale", "uncertain"},
    "blocked": {"stale"},
    "stale": {"ready", "blocked"},
    "failed": set(),
    "cancelled": set(),
}


def transition(
    plan: Plan,
    node_id: str,
    status: str,
    *,
    attempt_id: str = "",
    expected_version: int | None = None,
    evidence=None,
    **fields,
) -> Plan:
    node = plan.node(node_id)
    if expected_version is not None and expected_version != plan.plan_version:
        raise ValueError("late_plan_result")
    if status not in TRANSITIONS[node.status]:
        raise ValueError(f"invalid_node_transition: {node.status} -> {status}")
    if node.attempt_id and status != "running" and attempt_id != node.attempt_id:
        raise ValueError("late_attempt_result")
    if status == "running":
        if not attempt_id:
            raise ValueError("attempt_identity_required")
        fields.update(attempt_id=attempt_id, started_at=time.time())
    if status == "succeeded":
        refs = tuple(fields.get("output_evidence_refs", node.output_evidence_refs))
        if evidence is None or not evidence.completion(node, refs, attempt_id):
            raise ValueError("completion_unsatisfied")
    if status in {"succeeded", "failed", "cancelled", "uncertain"}:
        fields["ended_at"] = time.time()
    new_node = replace(node, status=status, **fields)
    nodes = tuple(new_node if n.node_id == node_id else n for n in plan.nodes)
    depended_on = {dependency for n in nodes for dependency in n.depends_on}
    leaves = [n for n in nodes if n.node_id not in depended_on]
    completed = all(n.status == "succeeded" for n in leaves) and all(
        n.status == "succeeded" or (n.status == "stale" and n.kind in {"explore", "analyze"})
        for n in nodes
    )
    overall = "completed" if completed else "active"
    if any(n.status == "uncertain" for n in nodes):
        overall = "uncertain"
    return replace(plan, nodes=nodes, state_revision=plan.state_revision + 1, status=overall).seal()


def refresh(plan: Plan, evidence) -> Plan:
    """Propagate failure, then admit only nodes with fresh required inputs."""
    changed = True
    while changed:
        changed = False
        for node in plan.nodes:
            if node.status not in {"pending", "ready"}:
                continue
            dependencies = [plan.node(key) for key in node.depends_on]
            bad = [
                d.node_id
                for d in dependencies
                if d.status
                in {
                    "failed",
                    "cancelled",
                    "blocked",
                    "uncertain",
                    "stale",
                }
            ]
            reason = ""
            if bad:
                reason = "dependency_not_succeeded: " + ",".join(bad)
            elif node.input_evidence_refs and not all(
                evidence.valid(ref) for ref in node.input_evidence_refs
            ):
                reason = "input_evidence_stale_or_unknown"
            if reason:
                plan = transition(
                    plan, node.node_id, "blocked", attempt_id=node.attempt_id, failure=reason
                )
                changed = True
            elif node.status == "pending" and all(d.status == "succeeded" for d in dependencies):
                plan = transition(plan, node.node_id, "ready")
                changed = True
    return plan


def restart_safe(plan: Plan, node_id: str, *, attempt_id: str, reason: str) -> Plan:
    """Recovery-only reset after old read/verify execution is proven stopped."""
    node = plan.node(node_id)
    if node.attempt_id and node.attempt_id != attempt_id:
        raise ValueError("late_attempt_result")
    if node.kind == "edit" and reason != "prepared_not_dispatched":
        raise ValueError("write_replay_forbidden")
    new_node = replace(
        node,
        status="pending",
        attempt_id="",
        failure=reason,
        output_evidence_refs=(),
        receipt_refs=(),
        started_at=0,
        ended_at=0,
    )
    return replace(
        plan,
        nodes=tuple(new_node if n.node_id == node_id else n for n in plan.nodes),
        state_revision=plan.state_revision + 1,
        status="active",
    ).seal()
