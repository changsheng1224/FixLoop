"""Small, detached delegation contexts; never a second Plan authority."""

from __future__ import annotations

import json
from copy import deepcopy

from agent_runtime.plan_runtime.models import digest

CONTEXT_MAX_BYTES = 8000


def delegation_context(session, plan):
    if session is None:
        return {}
    source = session.build_long_task_context(plan["node_id"])
    view, node = source["plan_view"], source["current_node"]
    if (
        not node
        or any(view[k] != plan[k] for k in ("plan_id", "plan_version"))
        or (plan["node_id"] and node["node_id"] != plan["node_id"])
        or not source["original_request"]
    ):
        raise ValueError("exploration_plan_context_invalid")
    result = deepcopy(
        {
            "schema_version": "delegation-context-v1",
            "goal": source["original_request"],
            "hard_constraints": source["hard_constraints"],
            "plan_view": {k: v for k, v in view.items() if k not in {"nodes", "active_node_ids"}},
            "current_node": node,
            "task_state_revision": source["state_revision"],
        }
    )
    result["checksum"] = digest(result)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > CONTEXT_MAX_BYTES:
        raise ValueError("exploration_context_budget_exceeded")
    return result


def context_valid(context, data):
    # Standalone runtime can operate without a Plan; L2 delegates always bind one.
    if not context:
        return not data["plan_id"]
    view = context.get("plan_view", {})
    return bool(
        context.get("checksum") == digest({k: v for k, v in context.items() if k != "checksum"})
        and all(view.get(k) == data[k] for k in ("plan_id", "plan_version", "workspace_id"))
        and context.get("current_node", {}).get("node_id") == data["node_id"]
    )
