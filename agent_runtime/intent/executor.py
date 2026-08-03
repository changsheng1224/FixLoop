"""Thin serial IntentGraph executor (P1 — no parallel scheduling)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agent_runtime.intent.graph import merge_constraints, topological_executable_nodes
from agent_runtime.intent.models import IntentNode, IntentResult

Handler = Callable[[IntentNode], Any]


@dataclass
class StepOutcome:
    node_id: str
    action: str
    ok: bool
    result: Any = None
    error: str | None = None


@dataclass
class ExecutorReport:
    outcomes: list[StepOutcome] = field(default_factory=list)
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return (not self.aborted) and all(o.ok for o in self.outcomes)


class IntentGraphExecutor:
    """Topological serial runner; fail-fast on first handler error."""

    def __init__(self, handlers: dict[str, Handler] | None = None) -> None:
        self.handlers = handlers or {}

    def serial(self, result: IntentResult) -> ExecutorReport:
        action = result.action
        # short-circuit non-graph actions that are themselves terminal
        if action in ("clarify", "help", "reject", "noop_cancel") and result.graph.mode != "multi":
            node = result.graph.nodes[0] if result.graph.nodes else None
            if node is None:
                return ExecutorReport(
                    outcomes=[
                        StepOutcome("n0", action, False, error="missing node")
                    ],
                    aborted=True,
                )
            return self._run_one(node)

        graph = merge_constraints(result.graph)
        nodes = topological_executable_nodes(graph)
        report = ExecutorReport()
        for node in nodes:
            # assert constrains merged: constraint roles not in list
            assert node.role == "executable"
            outcome = self._dispatch(node)
            report.outcomes.append(outcome)
            if not outcome.ok:
                report.aborted = True
                break
        return report

    def _run_one(self, node: IntentNode) -> ExecutorReport:
        outcome = self._dispatch(node)
        return ExecutorReport(outcomes=[outcome], aborted=not outcome.ok)

    def _dispatch(self, node: IntentNode) -> StepOutcome:
        handler = self.handlers.get(node.action) or self.handlers.get(node.primary)
        if handler is None:
            return StepOutcome(
                node.id,
                node.action,
                False,
                error=f"no handler for action={node.action}",
            )
        try:
            out = handler(node)
            return StepOutcome(node.id, node.action, True, result=out)
        except Exception as exc:  # fail-fast
            return StepOutcome(node.id, node.action, False, error=str(exc))
