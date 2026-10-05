"""模型可见的工具 schema 视图（不含 run 实现指针）。"""

from __future__ import annotations

import json
from functools import lru_cache

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import Unresolvable

__all__ = [
    "TOOL_SPEC_PUBLIC_KEYS",
    "schema_to_json",
    "tool_schema_view",
    "validate_tool_arguments",
]

TOOL_SPEC_PUBLIC_KEYS = frozenset({"schema", "description", "risky"})


def schema_to_json(schema: dict) -> dict:
    """Validate the single object JSON Schema accepted by the runtime."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("tool schema must be an object JSON Schema")
    result = dict(schema)
    result.setdefault("properties", {})
    result.setdefault("required", [])
    _validator_for(json.dumps(result, sort_keys=True))
    return result


@lru_cache(maxsize=256)
def _validator_for(serialized: str) -> Draft202012Validator:
    schema = json.loads(serialized)
    dialect = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    if dialect.rstrip("#") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("tool schemas must use JSON Schema Draft 2020-12")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=Registry())


def validate_tool_arguments(schema: dict, arguments: dict) -> tuple[dict, list[dict]]:
    """Validate Draft 2020-12 without coercion or remote reference retrieval."""
    schema = schema_to_json(schema)
    if not isinstance(arguments, dict):
        return {}, [{"code": "arguments_not_object", "message": "tool arguments must be an object"}]
    return dict(arguments), validate_json_value(schema, arguments)


def validate_json_value(schema: dict, value) -> list[dict]:
    """Validate a JSON value without coercion, using the runtime schema dialect."""
    codes = {
        "required": "missing_required_argument",
        "additionalProperties": "unknown_argument",
        "type": "invalid_argument_type",
        "enum": "enum_violation",
        "minimum": "minimum_violation",
        "maximum": "maximum_violation",
        "pattern": "pattern_violation",
        "minLength": "min_length",
        "maxLength": "max_length",
        "minItems": "min_items",
        "maxItems": "max_items",
    }
    validator = _validator_for(json.dumps(schema, sort_keys=True))
    try:
        violations = list(validator.iter_errors(value))
    except Unresolvable as exc:
        return [{"code": "unresolvable_schema_reference", "field": "", "message": str(exc)}]
    errors = [
        {
            "code": codes.get(error.validator, f"{error.validator}_violation"),
            "field": ".".join(map(str, error.absolute_path)),
            "message": error.message,
        }
        for error in violations
    ]
    return errors


def tool_schema_view(registry: dict) -> dict[str, dict]:
    """返回供 prompt / native API 使用的工具描述（仅 schema 相关字段）。"""
    view: dict[str, dict] = {}
    for name, spec in registry.items():
        view[name] = {key: spec[key] for key in TOOL_SPEC_PUBLIC_KEYS if key in spec}
        view[name]["schema"] = schema_to_json(spec["schema"])
    return view
