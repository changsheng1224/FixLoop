"""Reject unsupported graph semantics and understated tool effects."""

from __future__ import annotations

import json

from .models import Plan

MAX_NODES = 8
MAX_DEPENDENCIES = 3
MAX_PARALLEL_READS = 2
MAX_REPLANS = 2
COMPLETIONS = {
    "explore": "observation_present",
    "analyze": "analysis_recorded",
    "edit": "patch_applied",
    "verify": "tests_passed",
}
STATUSES = {
    "pending",
    "ready",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
    "stale",
    "uncertain",
}


def tool_effect(name: str, registry: dict) -> str:
    spec = registry.get(name)
    if spec is None:
        raise ValueError(f"unknown_tool: {name}")
    # Explicit trusted metadata only. Missing metadata is conservative.
    effect = spec.get("side_effect", "write")
    if effect == "external" or name in {
        "run_shell",
        "quick_test",
        "sandbox_build",
        "sandbox_test",
        "sandbox_verify",
    }:
        effect = "verify"
    if name in {"write_file", "patch_file", "apply_patch", "expand_lock"}:
        effect = "write"
    if effect not in {"read", "write", "verify"}:
        raise ValueError(f"invalid_tool_effect: {name}")
    return effect


def validate_plan(plan: Plan, registry: dict, *, identity: dict | None = None) -> None:
    if plan.schema_version != "1" or not plan.verify():
        raise ValueError("plan_schema_or_checksum_invalid")
    if plan.plan_version < 1 or plan.state_revision < 0:
        raise ValueError("plan_revision_invalid")
    for key in ("plan_id", "task_id", "run_id", "workspace_id", "session_id"):
        if not getattr(plan, key):
            raise ValueError(f"missing_identity: {key}")
        if identity and key in identity and getattr(plan, key) != identity[key]:
            raise ValueError(f"plan_identity_mismatch: {key}")
    if not 1 <= len(plan.nodes) <= MAX_NODES:
        raise ValueError("node_budget_exceeded")
    by_id = {node.node_id: node for node in plan.nodes}
    if len(by_id) != len(plan.nodes) or any(not key for key in by_id):
        raise ValueError("duplicate_or_empty_node_id")
    for node in plan.nodes:
        if node.kind not in COMPLETIONS or node.status not in STATUSES:
            raise ValueError(f"invalid_node: {node.node_id}")
        if (
            node.side_effect
            != {"explore": "read", "analyze": "read", "edit": "write", "verify": "verify"}[
                node.kind
            ]
        ):
            raise ValueError(f"node_effect_mismatch: {node.node_id}")
        if not node.objective or not node.completion:
            raise ValueError(f"completion_required: {node.node_id}")
        if any(c.kind != COMPLETIONS[node.kind] for c in node.completion):
            raise ValueError(f"completion_type_invalid: {node.node_id}")
        if len(node.depends_on) > MAX_DEPENDENCIES:
            raise ValueError("dependency_budget_exceeded")
        if len(set(node.depends_on)) != len(node.depends_on):
            raise ValueError("duplicate_dependency")
        if any(dep not in by_id for dep in node.depends_on):
            raise ValueError(f"unknown_dependency: {node.node_id}")
        if len(set(node.tool_allowlist)) != len(node.tool_allowlist):
            raise ValueError("duplicate_tool")
        for name in node.tool_allowlist:
            effect = tool_effect(name, registry)
            if node.kind in {"explore", "analyze"} and effect != "read":
                raise ValueError(f"readonly_tool_violation: {name}")
        if node.tool_name and node.tool_name not in node.tool_allowlist:
            raise ValueError("operation_not_allowed")
        if node.kind == "explore" and not node.tool_name:
            raise ValueError("explore_requires_fixed_operation")
        if not isinstance(json.loads(node.arguments_json), dict):
            raise ValueError("operation_arguments_invalid")
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            raise ValueError("plan_cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_id[key].depends_on:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in by_id:
        visit(key)
