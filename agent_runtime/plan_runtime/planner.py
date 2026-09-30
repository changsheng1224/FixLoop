"""A bounded main-agent planning output grounded in actual observations."""

from __future__ import annotations

import json
from dataclasses import replace

from agent_runtime.context_runtime import ObservationStore

from .models import Completion, Plan, PlanNode, new_id
from .validate import tool_effect, validate_plan


def grounded_plan(
    session,
    operations: list[dict],
    evidence_refs: list[str],
    *,
    objective: str,
    light_client=None,
    plan_id: str = "",
) -> tuple[Plan, str]:
    reads = tuple(
        name for name in session.registry if tool_effect(name, session.registry) == "read"
    )
    edits = tuple(
        name for name in session.registry if tool_effect(name, session.registry) != "verify"
    )
    nodes = []
    for index, operation in enumerate(operations[:2]):
        nodes.append(
            PlanNode(
                f"explore-{index + 1}",
                "explore",
                "Inspect repository evidence: " + operation["tool"],
                tool_allowlist=(operation["tool"],),
                completion=(Completion("observation_present"),),
                tool_name=operation["tool"],
                arguments_json=json.dumps(operation["arguments"]),
            )
        )
    nodes.append(
        PlanNode(
            "analyze",
            "analyze",
            "Record the repair hypothesis with repository evidence",
            depends_on=tuple(n.node_id for n in nodes),
            tool_allowlist=reads,
            completion=(Completion("analysis_recorded"),),
            input_evidence_refs=tuple(evidence_refs),
        )
    )
    nodes.append(
        PlanNode(
            "edit",
            "edit",
            objective,
            depends_on=("analyze",),
            tool_allowlist=edits,
            side_effect="write",
            completion=(Completion("patch_applied"),),
        )
    )
    nodes.append(
        PlanNode(
            "verify",
            "verify",
            "Run the existing final verifier against the applied patch",
            depends_on=("edit",),
            side_effect="verify",
            completion=(Completion("tests_passed"),),
        )
    )
    plan = Plan(plan_id or new_id("plan"), **session.identity, nodes=tuple(nodes)).seal()
    conclusion = ""
    if light_client is not None:
        summaries = []
        for ref in evidence_refs:
            record = session.evidence.get(ref)
            observations = ObservationStore(
                {
                    "id": session.identity["session_id"],
                    "session_scope": {"session_id": session.identity["session_id"]},
                },
                session.workspace,
                session.state_root,
            )
            try:
                observation = observations.get(record["observation_id"])
                summary = (
                    observation.summary
                    if observation
                    else record.get("observation_record", {}).get("summary", "")
                )
            finally:
                observations.close()
            summaries.append(
                {
                    "evidence_id": ref,
                    "observation_id": record["observation_id"],
                    "files": list(record["file_versions"]),
                    "summary": summary,
                }
            )
        prompt = (
            "Create a small repair task DAG using only the cited repository observations. "
            "Unknown locations remain hypotheses requiring explore nodes. "
            'Return JSON {"conclusion":"...","nodes":[...]} with the supplied node '
            "fields. Keep one analyze, one edit, one final verify node. "
            "No commands in read nodes.\n"
            + json.dumps(
                {
                    "objective": objective[:4000],
                    "evidence": summaries,
                    "nodes": [n.definition() for n in plan.nodes],
                },
                ensure_ascii=False,
            )
        )
        try:
            raw = light_client.complete(prompt, max_new_tokens=1800)
            candidate = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
            payload = plan.to_dict()
            payload["nodes"] = candidate.get("nodes", payload["nodes"])
            generated = Plan.from_dict(payload).seal()
            validate_plan(generated, session.registry, identity=session.identity)
            for kind in ("analyze", "edit", "verify"):
                if len([n for n in generated.nodes if n.kind == kind]) != 1:
                    raise ValueError("repair_plan_requires_single_owner_stage")
            if any(n.status != "pending" for n in generated.nodes):
                raise ValueError("model_cannot_set_execution_state")
            if any(
                ref not in evidence_refs for n in generated.nodes for ref in n.input_evidence_refs
            ):
                raise ValueError("ungrounded_plan_evidence")
            conclusion = str(candidate["conclusion"]).strip()
            if not conclusion:
                raise ValueError("analysis_conclusion_required")
            by_kind = {n.kind: n for n in generated.nodes if n.kind != "explore"}

            def ancestors(node):
                result = set(node.depends_on)
                for dep in node.depends_on:
                    result.update(ancestors(generated.node(dep)))
                return result

            if by_kind["analyze"].node_id not in ancestors(by_kind["edit"]) or by_kind[
                "edit"
            ].node_id not in ancestors(by_kind["verify"]):
                raise ValueError("repair_stage_dependency_missing")
            plan = generated
        except (ValueError, KeyError, TypeError) as exc:
            session.emit("plan_candidate_rejected", reason=str(exc))
    validate_plan(plan, session.registry, identity=session.identity)
    session.store.append(
        "planning",
        {
            "plan_version": session.plan.plan_version + 1 if session.plan else 1,
            "conclusion": conclusion,
            "evidence_refs": evidence_refs,
        },
    )
    return plan, conclusion


def retry_plan(session, operations, refs, *, objective, reason, light_client=None):
    from .replan import replan

    candidate, conclusion = grounded_plan(
        session,
        operations,
        refs,
        objective=objective,
        light_client=light_client,
        plan_id=session.plan.plan_id,
    )
    # New semantics get new IDs. Successful edits remain immutable history.
    version = session.plan.plan_version + 1
    mapping = {n.node_id: f"{n.node_id}-v{version}" for n in candidate.nodes}
    fresh = tuple(
        replace(n, node_id=mapping[n.node_id], depends_on=tuple(mapping[d] for d in n.depends_on))
        for n in candidate.nodes
    )
    replan(session, replace(candidate, nodes=fresh).seal(), reason=reason, evidence_refs=refs)
    return conclusion
