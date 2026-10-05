"""Derive strict JSON Schemas from dataclass argument definitions."""

from dataclasses import MISSING, fields
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from agent_runtime.tool_schema import validate_tool_arguments


def auto_schema(args_cls: type) -> dict:
    hints = get_type_hints(args_cls)
    properties, required = {}, []
    for item in fields(args_cls):
        schema = _type_schema(hints[item.name])
        if item.default is not MISSING:
            schema["default"] = item.default
        elif item.default_factory is MISSING:
            required.append(item.name)
        properties[item.name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _type_schema(hint) -> dict:
    origin, args = get_origin(hint), get_args(hint)
    if hint is Any:
        return {}
    if origin in (Union, UnionType):
        return {"anyOf": [_type_schema(item) for item in args]}
    if origin is Literal:
        return {"enum": list(args)}
    if origin is list or hint is list:
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    if origin is dict or hint is dict:
        return {"type": "object", "additionalProperties": _type_schema(args[1]) if args else True}
    types = {str: "string", int: "integer", float: "number", bool: "boolean", type(None): "null"}
    if hint not in types:
        raise TypeError(f"unsupported tool argument type: {hint!r}")
    return {"type": types[hint]}


def auto_validate(args_cls: type, args: dict) -> dict:
    validated, errors = validate_tool_arguments(auto_schema(args_cls), args)
    if errors:
        raise ValueError(f"invalid tool arguments: {errors}")
    return validated
