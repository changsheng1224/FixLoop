"""模型可见的工具 schema 视图（不含 run 实现指针）。"""

from __future__ import annotations

__all__ = [
    "TOOL_SPEC_PUBLIC_KEYS",
    "schema_to_json",
    "tool_schema_view",
    "validate_tool_arguments",
]

TOOL_SPEC_PUBLIC_KEYS = frozenset({"schema", "description", "risky"})


def schema_to_json(schema: dict) -> dict:
    """Convert the legacy compact schema to one provider-neutral JSON schema."""
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
    normalized = dict(arguments)
    errors = []
    for name, raw in schema.items():
        kind, sep, default = str(raw).partition("=")
        if name not in normalized:
            if sep:
                continue
            errors.append(
                {"code": "missing_required_argument", "field": name, "expected": kind}
            )
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


def tool_schema_view(registry: dict) -> dict[str, dict]:
    """返回供 prompt / native API 使用的工具描述（仅 schema 相关字段）。"""
    view: dict[str, dict] = {}
    for name, spec in registry.items():
        view[name] = {key: spec[key] for key in TOOL_SPEC_PUBLIC_KEYS if key in spec}
    return view
