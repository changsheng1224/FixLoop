"""Bounded Tool DAG execution with safe read parallelism."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.tool_result import ToolErrorCode, ToolResult, ToolStatus, normalize_tool_result


@dataclass(frozen=True)
class ToolNode:
    node_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    side_effect: str = "read"


class ToolDAGExecutor:
    """Execute independent reads in parallel and serialize side effects."""

    def __init__(self, execute: Callable[[str, dict[str, Any]], Any], *, max_workers: int = 4):
        self.execute = execute
        self.max_workers = max(1, int(max_workers))

    def run(self, nodes: list[ToolNode]) -> dict[str, ToolResult]:
        by_id = {node.node_id: node for node in nodes}
        if len(by_id) != len(nodes):
            raise ValueError("duplicate tool DAG node id")
        unknown = {
            dependency
            for node in nodes
            for dependency in node.depends_on
            if dependency not in by_id
        }
        if unknown:
            raise ValueError(f"unknown tool DAG dependencies: {sorted(unknown)}")
        self._assert_acyclic(by_id)
        pending = set(by_id)
        results: dict[str, ToolResult] = {}
        while pending:
            ready = [
                by_id[node_id]
                for node_id in sorted(pending)
                if all(dependency in results for dependency in by_id[node_id].depends_on)
            ]
            if not ready:
                raise ValueError("tool DAG has an unresolved cycle")
            blocked = [
                node
                for node in ready
                if any(not results[dependency].ok for dependency in node.depends_on)
            ]
            for node in blocked:
                results[node.node_id] = ToolResult(
                    content=f"Error: dependency failed for {node.node_id}",
                    status=ToolStatus.REJECTED.value,
                    error_code=ToolErrorCode.STALE_PRECONDITION.value,
                    metadata={
                        "tool_status": ToolStatus.REJECTED.value,
                        "tool_error_code": ToolErrorCode.STALE_PRECONDITION.value,
                        "blocked_by": list(node.depends_on),
                    },
                )
            ready = [node for node in ready if node not in blocked]
            parallel = [node for node in ready if node.side_effect in {"read", "none"}]
            serial = [node for node in ready if node not in parallel]
            if parallel:
                with ThreadPoolExecutor(max_workers=min(self.max_workers, len(parallel))) as pool:
                    futures = {
                        node.node_id: pool.submit(self.execute, node.tool_name, node.arguments)
                        for node in parallel
                    }
                    for node in parallel:
                        results[node.node_id] = normalize_tool_result(
                            futures[node.node_id].result(), tool_name=node.tool_name
                        )
            for node in serial:
                results[node.node_id] = normalize_tool_result(
                    self.execute(node.tool_name, node.arguments), tool_name=node.tool_name
                )
            pending.difference_update(node.node_id for node in ready)
        return results

    @staticmethod
    def _assert_acyclic(by_id: dict[str, ToolNode]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("tool DAG contains a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in by_id[node_id].depends_on:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)


__all__ = ["ToolDAGExecutor", "ToolNode"]
