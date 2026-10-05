"""MCP argument validation through the runtime JSON Schema contract."""

from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

from agent_runtime.mcp.errors import McpSchemaError
from agent_runtime.tool_schema import validate_tool_arguments


def validate_arguments(*, tool_name, schema, arguments):
    try:
        args, errors = validate_tool_arguments(schema, {} if arguments is None else arguments)
    except (ValueError, SchemaError, Unresolvable) as exc:
        raise McpSchemaError(f"工具 '{tool_name}' schema 无效", detail=str(exc)) from exc
    if errors:
        raise McpSchemaError(f"工具 '{tool_name}' 参数不符合 schema", detail=str(errors))
    return args
