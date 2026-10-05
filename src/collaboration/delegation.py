"""Only the L2 owner receives the delegate/collect tool surface."""

from __future__ import annotations

import json

from agent_runtime.tool_result import ToolResult


def build_delegation_tools(context):
    def invoke(name, args):
        runtime = getattr(context, "readonly_exploration_runtime", None)
        if runtime is None:
            return ToolResult(
                content="Error: exploration requires an active L2 repair run",
                status="rejected",
                error_code="policy_denied",
            )
        try:
            value = (
                runtime.delegate(args["tasks"])
                if name == "delegate_exploration"
                else runtime.collect(args["handles"], args.get("wait_ms", 0))
            )
            return ToolResult(
                content=json.dumps(value, ensure_ascii=False),
                metadata={"termination_guaranteed": True},
            )
        except ValueError as exc:
            return ToolResult(
                content=f"Error: {exc}", status="rejected", error_code="policy_denied"
            )

    request = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "question"],
        "properties": {
            "kind": {"type": "string", "enum": ["implementation_location", "related_tests"]},
            "question": {"type": "string", "minLength": 1, "maxLength": 2000},
            "scope_paths": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "input_observation_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
    }
    schemas = {
        "delegate_exploration": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tasks"],
            "properties": {
                "tasks": {"type": "array", "items": request, "minItems": 1, "maxItems": 2}
            },
        },
        "collect_exploration": {
            "type": "object",
            "additionalProperties": False,
            "required": ["handles"],
            "properties": {
                "handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 2,
                },
                "wait_ms": {"type": "integer", "minimum": 0, "maximum": 1000},
            },
        },
    }
    descriptions = {
        "delegate_exploration": "Delegate at most two independent read-only tasks: implementation "
        "location and related test discovery. Returns stable handles. No recursive delegation.",
        "collect_exploration": "Collect scoped exploration handles, with at most 1000ms wait. "
        "Findings remain candidates: reread source and assess claims before updating Plan/editing.",
    }
    return {
        name: {
            "schema": schema,
            "description": descriptions[name],
            "risky": False,
            "side_effect": "read",
            "roles": ["patcher"],
            "budget_group": "read",
            "isolated_read": True,
            "execution_mode": "thread",
            "max_retries": 0,
            "run": lambda args, n=name: invoke(n, args),
        }
        for name, schema in schemas.items()
    }
