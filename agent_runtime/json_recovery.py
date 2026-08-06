"""Safe structural recovery for generic, tool-call and patch JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.response import CanonicalResponse


@dataclass
class StructuredParseResult:
    ok: bool
    value: Any = None
    error_code: str = ""
    repaired_text: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_response(self, kind: str = "final_answer") -> CanonicalResponse:
        payload = {"value": self.value} if self.ok else {}
        errors = [] if self.ok else [{"code": self.error_code}]
        return CanonicalResponse.create(
            kind,
            "success" if self.ok else "retry",
            payload,
            parse_errors=errors,
            warnings=list(self.warnings),
        )


def _unwrap(text: str) -> str:
    value = str(text or "").strip()
    final = re.search(r"<final>([\s\S]*?)</final>", value, re.IGNORECASE)
    if final:
        value = final.group(1).strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", value, re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    return value


def _balanced_json(text: str) -> tuple[str, bool]:
    starts = [(text.find("["), "[", "]"), (text.find("{"), "{", "}")]
    starts = [item for item in starts if item[0] >= 0]
    if not starts:
        return "", False
    start, opening, closing = min(starts, key=lambda item: item[0])
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"{": "}", "[": "]"}
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "}]":
            if not stack or stack[-1] != char:
                return "", False
            stack.pop()
            if not stack:
                return text[start : index + 1], True
    del opening, closing
    return text[start:], False


def repair_structured_output(
    raw: str,
    schema: dict | None = None,
    *,
    mode: str = "generic",
    max_attempts: int = 2,
) -> StructuredParseResult:
    del max_attempts
    text = _unwrap(raw)
    candidate, complete = _balanced_json(text)
    if not candidate:
        return StructuredParseResult(False, error_code="json_not_found")
    if not complete:
        return StructuredParseResult(False, error_code="truncated_json", repaired_text=candidate)
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
    warnings = ["trailing_comma_removed"] if repaired != candidate else []
    try:
        value = json.loads(repaired)
    except json.JSONDecodeError:
        return StructuredParseResult(False, error_code="invalid_json", repaired_text=repaired)
    if mode == "patch":
        rows = value.get("patches") if isinstance(value, dict) else value
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return StructuredParseResult(False, error_code="invalid_patch_shape")
        value = rows
    elif mode == "tool_call":
        if not isinstance(value, dict):
            return StructuredParseResult(False, error_code="invalid_tool_payload")
        name = value.get("name") or value.get("tool") or value.get("action")
        if not isinstance(name, str) or not name:
            return StructuredParseResult(False, error_code="missing_tool_name")
    if schema and isinstance(value, dict):
        from agent_runtime.tool_schema import validate_tool_arguments

        normalized, errors = validate_tool_arguments(schema, value)
        if errors:
            return StructuredParseResult(False, error_code="schema_validation_failed")
        value = normalized
    return StructuredParseResult(True, value, repaired_text=repaired, warnings=warnings)
