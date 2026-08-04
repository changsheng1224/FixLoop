"""MCP Tool Schema ↔ FixLoop 工具 schema 字符串映射。"""

from __future__ import annotations

from typing import Any


def json_schema_to_fixloop(
    properties: dict[str, Any], required: list[str] | None
) -> dict[str, str]:
    """将 JSON Schema properties 转为 FixLoop ``auto_schema`` 风格 dict。

    Example::
        {"owner": "str", "repo": "str", "limit": "int=30"}
    """
    req = set(required or [])
    out: dict[str, str] = {}
    for name, prop in (properties or {}).items():
        typ = _json_type_to_str(prop)
        if name not in req and "default" in prop:
            out[name] = f"{typ}={prop['default']}"
        elif name not in req:
            # 可选无默认：仍标类型，执行侧允许缺省
            out[name] = typ
        else:
            out[name] = typ
    return out


def _json_type_to_str(prop: dict[str, Any]) -> str:
    t = prop.get("type", "string")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    mapping = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "object": "str",
        "array": "str",
    }
    return mapping.get(str(t), "str")


def validate_arguments(
    *,
    tool_name: str,
    schema_props: dict[str, Any],
    required: list[str] | None,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """校验 call 参数；失败抛 ``McpSchemaError``。"""
    from agent_runtime.mcp.errors import McpSchemaError

    args = dict(arguments or {})
    req = list(required or [])
    missing = [k for k in req if k not in args or args[k] is None or args[k] == ""]
    if missing:
        raise McpSchemaError(
            f"工具 '{tool_name}' 缺少必填参数",
            detail=",".join(missing),
        )
    # 拒绝未声明字段（严格 Schema）
    allowed = set(schema_props or {})
    unknown = [k for k in args if k not in allowed]
    if unknown:
        raise McpSchemaError(
            f"工具 '{tool_name}' 含未知参数",
            detail=",".join(unknown),
        )
    return args
