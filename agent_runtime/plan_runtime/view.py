"""Detached, read-only projections of the authoritative Plan snapshot."""


def plan_view(plan, selected_node_id: str = "") -> dict:
    if not plan.verify():
        raise ValueError("plan_view_checksum_invalid")
    if selected_node_id and not any(n.node_id == selected_node_id for n in plan.nodes):
        raise ValueError("plan_view_node_missing")
    statuses = {n.node_id: n.status for n in plan.nodes}
    return {
        **{
            k: getattr(plan, k)
            for k in (
                "task_id",
                "run_id",
                "workspace_id",
                "plan_id",
                "plan_version",
                "state_revision",
                "plan_checksum",
            )
        },
        "selected_node_id": selected_node_id,
        "active_node_ids": [n.node_id for n in plan.nodes if n.status == "running"],
        "nodes": [
            {
                "node_id": n.node_id,
                "kind": n.kind,
                "objective": n.objective,
                "status": n.status,
                "completion": [
                    {"kind": c.kind, "evidence_refs": list(c.evidence_refs)} for c in n.completion
                ],
                "depends_on": list(n.depends_on),
                "dependency_statuses": {d: statuses[d] for d in n.depends_on},
                "input_evidence_refs": list(n.input_evidence_refs),
                "output_evidence_refs": list(n.output_evidence_refs),
                "block_or_failure_reason": n.failure,
            }
            for n in plan.nodes
        ],
    }
