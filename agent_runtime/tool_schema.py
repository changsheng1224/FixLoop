"""模型可见的工具 schema 视图（不含 run 实现指针）。"""

from __future__ import annotations

import re

__all__ = [
    "TOOL_SPEC_PUBLIC_KEYS",
    "schema_to_json",
    "tool_schema_view",
    "validate_tool_arguments",
]

TOOL_SPEC_PUBLIC_KEYS = frozenset({"schema", "json_schema", "description", "risky"})


def schema_to_json(schema: dict) -> dict:
    """Convert the legacy compact schema to one provider-neutral JSON schema."""
    if isinstance(schema, dict) and (schema.get("type") == "object" or "properties" in schema):
        result = dict(schema)
        result.setdefault("type", "object")
        result.setdefault("properties", {})
        result.setdefault("required", [])
        result.setdefault("additionalProperties", False)
        return result
    type_map = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "array": "array",
        "list": "array",
        "dict": "object",
    }
    properties, required = {}, []
    for name, raw in schema.items():
        kind, sep, default = str(raw).partition("=")
        prop = {"type": type_map.get(kind, "string")}
        if prop["type"] == "array":
            prop["items"] = {"type": "string"}
        if sep:
            prop["default"] = default
        else:
            required.append(name)
        properties[name] = prop
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_tool_arguments(schema: dict, arguments: dict) -> tuple[dict, list[dict]]:
    """Validate shape without deciding whether the tool is authorized."""
    if not isinstance(arguments, dict):
        return {}, [{"code": "arguments_not_object", "message": "tool arguments must be an object"}]
    if isinstance(schema, dict) and (schema.get("type") == "object" or "properties" in schema):
        normalized = dict(arguments)
        errors: list[dict] = []
        properties = dict(schema.get("properties") or {})
        for name in schema.get("required", []) or []:
            if name not in normalized:
                errors.append(
                    {
                        "code": "missing_required_argument",
                        "field": name,
                        "expected": properties.get(name, {}),
                    }
                )
        if schema.get("additionalProperties") is False:
            errors.extend(
                {"code": "unknown_argument", "field": name}
                for name in sorted(set(normalized) - set(properties))
            )
        for name, value in list(normalized.items()):
            if name not in properties:
                continue
            normalized[name], field_errors = _validate_json_value(
                value, properties[name], field=name
            )
            errors.extend(field_errors)
        return normalized, errors
    normalized = dict(arguments)
    errors = []
    for name, raw in schema.items():
        kind, sep, default = str(raw).partition("=")
        if name not in normalized:
            if sep:
                continue
            errors.append({"code": "missing_required_argument", "field": name, "expected": kind})
            continue
        value = normalized[name]
        expected = {
            "str": str,
            "int": int,
            "float": (int, float),
            "bool": bool,
            "array": list,
            "list": list,
            "dict": dict,
        }.get(kind)
        if expected and not isinstance(value, expected):
            try:
                if kind == "int":
                    normalized[name] = int(value)
                elif kind == "float":
                    normalized[name] = float(value)
                elif kind == "str":
                    normalized[name] = str(value)
                else:
                    raise TypeError
            except (TypeError, ValueError):
                errors.append(
                    {
                        "code": "invalid_argument_type",
                        "field": name,
                        "expected": kind,
                        "actual": type(value).__name__,
                    }
                )
    unknown = sorted(set(normalized) - set(schema))
    errors.extend({"code": "unknown_argument", "field": name} for name in unknown)
    return normalized, errors


def _validate_json_value(value, schema: dict, *, field: str) -> tuple[object, list[dict]]:
    """Small dependency-free JSON Schema validator for tool arguments."""
    if not isinstance(schema, dict):
        return value, []
    errors: list[dict] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append({"code": "enum_violation", "field": field, "allowed": schema["enum"]})
        return value, errors
    expected = schema.get("type")
    valid = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
    }
    if expected in valid and not valid[expected](value):
        errors.append({"code": "invalid_argument_type", "field": field, "expected": expected})
        return value, errors
    if isinstance(value, str):
        if schema.get("pattern") and not re.search(str(schema["pattern"]), value):
            errors.append({"code": "pattern_violation", "field": field})
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append({"code": "min_length", "field": field})
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append({"code": "max_length", "field": field})
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append({"code": "minimum_violation", "field": field})
        if "maximum" in schema and value > schema["maximum"]:
            errors.append({"code": "maximum_violation", "field": field})
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _, item_errors = _validate_json_value(item, schema["items"], field=f"{field}[{index}]")
            errors.extend(item_errors)
    if isinstance(value, dict):
        nested, nested_errors = validate_tool_arguments(schema, value)
        errors.extend(
            {**error, "field": f"{field}.{error.get('field', '')}".rstrip(".")}
            for error in nested_errors
        )
        value = nested
    return value, errors


def tool_schema_view(registry: dict) -> dict[str, dict]:
    """返回供 prompt / native API 使用的工具描述（仅 schema 相关字段）。"""
    view: dict[str, dict] = {}
    for name, spec in registry.items():
        view[name] = {key: spec[key] for key in TOOL_SPEC_PUBLIC_KEYS if key in spec}
        if "json_schema" in spec:
            view[name]["json_schema"] = schema_to_json(spec["json_schema"])
    return view
