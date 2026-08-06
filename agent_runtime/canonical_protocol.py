"""Single canonical parser and retry policy for provider/tool protocols."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_runtime.json_recovery import repair_structured_output
from agent_runtime.repair_runtime import CanonicalToolCall, ToolSource
from agent_runtime.response import CanonicalResponse


class ToolErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    PERMISSION_DENIED = "permission_denied"
    POLICY_DENIED = "policy_denied"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    PROVIDER_PROTOCOL_ERROR = "provider_protocol_error"
    MCP_UNAVAILABLE = "mcp_unavailable"


@dataclass(frozen=True)
class ToolCallDecision:
    status: str
    error_code: str = ""
    retryable: bool = False
    retry_limit: int = 0
    model_hint: str = ""


_ERROR_POLICY = {
    ToolErrorCode.INVALID_JSON.value: (True, 2, "Return one complete valid tool call."),
    ToolErrorCode.INVALID_ARGUMENTS.value: (True, 2, "Correct arguments using the tool schema."),
    ToolErrorCode.UNKNOWN_TOOL.value: (
        False,
        0,
        "Choose a tool from the available capability list.",
    ),
    ToolErrorCode.PERMISSION_DENIED.value: (False, 0, "Use an allowed alternative tool."),
    ToolErrorCode.POLICY_DENIED.value: (False, 0, "Satisfy the runtime precondition first."),
    ToolErrorCode.TOOL_TIMEOUT.value: (True, 1, "Retry once with a narrower request."),
    ToolErrorCode.MCP_UNAVAILABLE.value: (
        True,
        2,
        "Retry later or continue without the unavailable MCP capability.",
    ),
}


def decide_tool_error(error_code: str) -> ToolCallDecision:
    retryable, limit, hint = _ERROR_POLICY.get(
        str(error_code), (False, 0, "Stop and revise the plan.")
    )
    return ToolCallDecision(
        "retry" if retryable else "stop", str(error_code), retryable, limit, hint
    )


def parse_tool_call(
    raw: Any,
    *,
    source: str = "text",
    expected_tools: set[str] | None = None,
) -> CanonicalResponse:
    if hasattr(raw, "name") and hasattr(raw, "arguments"):
        call = CanonicalToolCall.create(
            raw.name,
            raw.arguments,
            source=ToolSource.NATIVE,
            call_id=getattr(raw, "call_id", ""),
        )
        return _call_response(call, expected_tools)
    text = str(raw or "").strip()
    match = re.search(r"<function_calls>([\s\S]*?)</function_calls>", text, re.I)
    if match:
        invoke = re.search(r'<invoke\s+name="([^"]+)">([\s\S]*?)</invoke>', match.group(1), re.I)
        if invoke:
            args = {
                item.group(1): item.group(2).strip()
                for item in re.finditer(
                    r'<parameter\s+name="([^"]+)">([\s\S]*?)</parameter>',
                    invoke.group(2),
                    re.I,
                )
            }
            call = CanonicalToolCall.create(invoke.group(1), args, source=ToolSource.RECOVERED)
            return _call_response(call, expected_tools)
    inner = re.search(r"<tool>([\s\S]*?)</tool>", text, re.I)
    attribute_tool = re.search(
        r'<tool\s+name="([^"]+)"([^>]*)>([\s\S]*?)</tool>', text, re.I
    )
    if attribute_tool:
        call = CanonicalToolCall.create(
            attribute_tool.group(1),
            {
                "attrs": attribute_tool.group(2).strip(),
                "body": attribute_tool.group(3).strip(),
            },
            source=ToolSource.RECOVERED,
        )
        return _call_response(call, expected_tools)
    candidate = inner.group(1) if inner else text
    parsed = repair_structured_output(candidate, mode="tool_call")
    if not parsed.ok:
        if inner and parsed.error_code == "missing_tool_name":
            recovered = repair_structured_output(candidate)
            return CanonicalResponse.create(
                "error",
                "retry",
                {
                    "error_code": "invalid_tool_payload",
                    "value": recovered.value if recovered.ok else {},
                },
                parse_errors=[{"code": "invalid_tool_payload"}],
            )
        decision = decide_tool_error(
            ToolErrorCode.INVALID_JSON.value
            if parsed.error_code in {"invalid_json", "truncated_json"}
            else ToolErrorCode.PROVIDER_PROTOCOL_ERROR.value
        )
        return CanonicalResponse.create(
            "error",
            decision.status,
            {"decision": decision.__dict__},
            parse_errors=[{"code": parsed.error_code}],
        )
    payload = parsed.value
    if not isinstance(payload, dict):
        return CanonicalResponse.create("structured", "success", {"value": payload})
    name = payload.get("name") or payload.get("tool") or payload.get("action")
    if not isinstance(name, str) or not name.strip():
        return CanonicalResponse.create(
            "error",
            "retry",
            {"error_code": "invalid_tool_payload", "value": payload},
            parse_errors=[{"code": "invalid_tool_payload"}],
        )
    call = CanonicalToolCall.create(
        name,
        payload.get("args") or payload.get("arguments") or payload.get("parameters") or {},
        source=ToolSource.TEXT if source == "text" else ToolSource.RECOVERED,
    )
    return _call_response(call, expected_tools)


def parse_model_response(
    raw: Any,
    *,
    expected_tools: set[str] | None = None,
) -> CanonicalResponse:
    """Normalize one text-protocol model response into the runtime envelope."""
    text = str(raw or "").strip()
    final_match = re.search(r"<final>([\s\S]*?)</final>", text, re.I)
    if final_match:
        return CanonicalResponse.create(
            "final", "success", {"text": final_match.group(1).strip()}
        )

    response = parse_tool_call(text, expected_tools=expected_tools)
    if response.payload.get("error_code") == "invalid_tool_payload":
        return response
    if response.response_kind == "structured":
        import json

        return CanonicalResponse.create(
            "final",
            "success",
            {"text": json.dumps(response.payload.get("value"), ensure_ascii=False)},
            warnings=response.warnings,
        )
    if response.response_kind == "error" and text.startswith(("{", "[")):
        recovered = repair_structured_output(text)
        if recovered.ok:
            import json

            return CanonicalResponse.create(
                "final",
                "success",
                {"text": json.dumps(recovered.value, ensure_ascii=False)},
                warnings=recovered.warnings,
            )
    return response


def _call_response(call: CanonicalToolCall, expected_tools: set[str] | None) -> CanonicalResponse:
    if expected_tools is not None and call.name not in expected_tools:
        decision = decide_tool_error(ToolErrorCode.UNKNOWN_TOOL.value)
        return CanonicalResponse.create("error", "stop", {"decision": decision.__dict__})
    return CanonicalResponse.create("tool_call", "success", {"call": call})
