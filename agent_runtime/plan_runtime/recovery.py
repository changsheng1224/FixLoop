"""Reconstruct facts across every dispatch/result/reducer/checkpoint window."""

from __future__ import annotations

from .processes import confirmed_exited
from .reducer import restart_safe, transition
from .workspace import changes, snapshot


def recover(session, *, stopped_probe=None) -> dict:
    session.store.validate_execution_history()
    report = {
        key: []
        for key in (
            "adopted",
            "restarted_read",
            "rerun_verify",
            "stale",
            "uncertain",
            "blocked",
            "resume_rejected",
        )
    }
    checkpoints = [e["payload"] for e in session.store.events() if e["kind"] == "checkpoint"]
    for seal in checkpoints:
        session.store.verify_checkpoint(seal)
    attempts = session.store.latest("attempt", "attempt_id")
    current = snapshot(session.workspace)
    for attempt in attempts.values():
        if attempt["phase"] == "reconciled":
            continue
        if attempt["plan_version"] == 0:
            # A pre-plan read journal is never permission to edit.
            operations = session.operations(attempt["attempt_id"])
            stopped = (
                stopped_probe(attempt)
                if stopped_probe
                else confirmed_exited(attempt.get("owner", {}))
            )
            if attempt["phase"] == "result_recorded":
                stopped = all(o.get("execution_stopped") for o in operations)
            if not stopped_probe and any(
                o["phase"] == "dispatched" and o["tool"] not in {"read_file", "list_files"}
                for o in operations
            ):
                stopped = False
            if attempt["phase"] == "prepared" or stopped:
                session.store.append(
                    "attempt", {**attempt, "phase": "reconciled", "terminal_status": "cancelled"}
                )
                report["restarted_read"].append(attempt["node_id"])
            else:
                report["uncertain"].append(attempt["node_id"])
            continue
        if session.plan is None:
            raise ValueError("resume_plan_missing")
        if (
            attempt["plan_id"] != session.plan.plan_id
            or attempt["plan_version"] != session.plan.plan_version
        ):
            raise ValueError("resume_attempt_identity_mismatch")
        node = session.plan.node(attempt["node_id"])
        if node.attempt_id and node.attempt_id != attempt["attempt_id"]:
            raise ValueError("resume_attempt_identity_mismatch")
        operations = session.operations(attempt["attempt_id"])
        prepared = attempt["phase"] == "prepared" and not operations
        if prepared:
            session.commit(
                restart_safe(
                    session.plan,
                    node.node_id,
                    attempt_id=attempt["attempt_id"],
                    reason="prepared_not_dispatched",
                )
            )
            session.store.append(
                "attempt", {**attempt, "phase": "reconciled", "terminal_status": "cancelled"}
            )
            report["restarted_read" if node.kind != "verify" else "rerun_verify"].append(
                node.node_id
            )
            continue
        complete_ops = all(
            o["phase"] == "result_recorded" and o.get("execution_stopped") for o in operations
        )
        owner_exited = confirmed_exited(attempt.get("owner", {}))
        stopped = bool(stopped_probe(attempt)) if stopped_probe else owner_exited
        if not stopped_probe and any(
            o["phase"] == "dispatched"
            and o["effect"] == "read"
            and o["tool"] not in {"read_file", "list_files"}
            for o in operations
        ):
            stopped = False
        # A process-exit lease cannot prove a shell/test descendant stopped.
        if node.kind == "verify" and attempt["phase"] != "result_recorded" and not stopped_probe:
            stopped = False
        if attempt["phase"] == "result_recorded":
            stopped = complete_ops and attempt["result"].get("status") != "uncertain"
        if node.kind == "edit" and operations and complete_ops and stopped:
            # Adopt only terminal writes with consistent post-state. Never guess
            # whether another write was about to happen inside a model callback.
            refs = session.tool_result(attempt)["evidence_refs"]
            if session.evidence.completion(node, tuple(refs), attempt["attempt_id"]):
                attempt = session.record_result(
                    attempt, {"status": "success", "evidence_refs": refs}
                )
        if attempt["phase"] == "result_recorded" and stopped:
            refs = tuple(attempt["result"].get("evidence_refs", ()))
            if attempt["result"].get("status") != "success" or session.evidence.completion(
                node, refs, attempt["attempt_id"]
            ):
                if node.status in {"ready", "pending"}:
                    if node.status == "pending":
                        session.commit(transition(session.plan, node.node_id, "ready"))
                    session.commit(
                        transition(
                            session.plan, node.node_id, "running", attempt_id=attempt["attempt_id"]
                        )
                    )
                session.settle(attempt)
                report["adopted"].append(node.node_id)
                continue
        if node.kind in {"explore", "analyze", "verify"} and stopped:
            session.commit(
                restart_safe(
                    session.plan,
                    node.node_id,
                    attempt_id=attempt["attempt_id"],
                    reason="old_execution_stopped",
                )
            )
            session.store.append(
                "attempt", {**attempt, "phase": "reconciled", "terminal_status": "cancelled"}
            )
            report["rerun_verify" if node.kind == "verify" else "restarted_read"].append(
                node.node_id
            )
        else:
            if node.status == "ready":
                session.commit(
                    transition(
                        session.plan, node.node_id, "running", attempt_id=attempt["attempt_id"]
                    )
                )
            if node.status == "running":
                session.commit(
                    transition(
                        session.plan,
                        node.node_id,
                        "uncertain",
                        attempt_id=attempt["attempt_id"],
                        failure="write_or_execution_outcome_unconfirmed",
                    )
                )
            report["uncertain"].append(
                {
                    "node_id": node.node_id,
                    "attempt_id": attempt["attempt_id"],
                    "changed_paths": changes(attempt["workspace_before"], current),
                }
            )
    if session.plan:
        for node in session.plan.nodes:
            if node.status != "succeeded":
                continue
            fresh = all(session.evidence.valid(r) for r in node.output_evidence_refs)
            if not fresh:
                if node.kind == "edit":
                    rollback = [
                        e["payload"]
                        for e in session.store.events()
                        if e["kind"] == "rollback"
                        and e["payload"].get("plan_version") == session.plan.plan_version
                    ]
                    if (
                        rollback
                        and rollback[-1].get("completed") is True
                        and rollback[-1].get("workspace_after") == current
                        and current == rollback[-1].get("expected_after")
                    ):
                        # A confirmed rollback retires the write only through a
                        # new graph; keep its immutable historical success here.
                        continue
                    status = "uncertain"
                else:
                    status = "stale"
                session.commit(
                    transition(
                        session.plan,
                        node.node_id,
                        status,
                        attempt_id=node.attempt_id,
                        failure="completed_evidence_no_longer_current",
                    )
                )
                report[status].append(node.node_id)
        session.refresh()
        report["blocked"] = [n.node_id for n in session.plan.nodes if n.status == "blocked"]
        recorded = {v["node_id"] if isinstance(v, dict) else v for v in report["uncertain"]}
        report["uncertain"].extend(
            n.node_id
            for n in session.plan.nodes
            if n.status == "uncertain" and n.node_id not in recorded
        )
    session.emit("plan_resume_reconciled", report=report)
    session.checkpoint()
    return report
