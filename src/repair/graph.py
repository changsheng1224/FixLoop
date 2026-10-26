"""Explicit repair stage topology with ephemeral routing and owner-only calls."""

from __future__ import annotations

from functools import cache
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from src.repair.graph_owner import OwnerCalls
from src.repair.graph_stages import RepairGraphContext, RepairStages


class RepairRoute(TypedDict):
    next_action: str


# Business decisions are committed by stages. Edges only read their result.
STAGE_ROUTES = {
    "entry": ("prepare", "attempt", "done"),
    "prepare": ("attempt",),
    "attempt": ("patch", "done"),
    "patch": ("critic", "empty_patch", "done"),
    "empty_patch": ("attempt", "done"),
    "critic": ("protected_interface", "attempt"),
    "protected_interface": ("verification_gate", "attempt"),
    "verification_gate": ("verify", "pending_verify"),
    "pending_verify": ("done",),
    "verify": ("verification_failure", "done"),
    "verification_failure": ("attempt", "done"),
}


def _stage_node(name):
    def node(route: RepairRoute, runtime: Runtime[RepairGraphContext]) -> RepairRoute:
        context = runtime.context
        return context.owner.call(lambda: {"next_action": getattr(RepairStages(context), name)()})

    return node


def _next_action(route: RepairRoute) -> str:
    return route["next_action"]


@cache
def repair_graph():
    builder = StateGraph(RepairRoute, context_schema=RepairGraphContext)
    for name, targets in STAGE_ROUTES.items():
        builder.add_node(name, _stage_node(name))
        builder.add_conditional_edges(
            name, _next_action, {target: END if target == "done" else target for target in targets}
        )
    builder.add_edge(START, "entry")
    return builder.compile()


def run_repair_graph(session, state, initial_snapshot, phase_clock, *, resume=False) -> bool:
    """Return cancellation to the existing lifecycle; never finalize resources here."""
    owner = OwnerCalls()
    context = RepairGraphContext(session, state, initial_snapshot, phase_clock, resume, owner)
    # Allow every stage per remaining business attempt plus startup and exit.
    # Framework defaults must not truncate a legal high-retry request.
    remaining = max(0, state.max_retries - state.retry_count)
    config = {"recursion_limit": (len(STAGE_ROUTES) + 2) * (remaining + 1)}
    owner.run(lambda: repair_graph().invoke({"next_action": "entry"}, config, context=context))
    return context.cancelled
