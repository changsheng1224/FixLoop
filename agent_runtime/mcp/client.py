"""进程内 MCP Client：``tools/list`` / ``tools/call``。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_runtime.mcp.errors import (
    McpError,
    McpSchemaError,
    McpTimeoutError,
    McpUnavailableError,
)
from agent_runtime.mcp.schema_map import validate_arguments


class McpTransport(Protocol):
    """可替换的 MCP 传输（进程内 / 未来 stdio）。"""

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ...


@dataclass
class McpToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def properties(self) -> dict[str, Any]:
        return dict(self.input_schema.get("properties") or {})

    @property
    def required(self) -> list[str]:
        return list(self.input_schema.get("required") or [])


@dataclass
class McpCallResult:
    """归一化后的 tools/call 结果。"""

    content: str
    is_error: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def observation(self) -> str:
        if self.is_error:
            return f"Error: {self.content}"
        return self.content


class InProcessTransport:
    """同步进程内 handler：``handler(method, params) -> result dict``。"""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.available = True

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.available:
            raise McpUnavailableError("MCP server 不可用")
        return self._handler(method, params or {})


class McpClient:
    """最小 MCP 客户端。"""

    def __init__(
        self,
        transport: McpTransport,
        *,
        timeout_s: float = 10.0,
        server_name: str = "github-mcp",
    ) -> None:
        self._transport = transport
        self.timeout_s = timeout_s
        self.server_name = server_name
        self._tool_cache: dict[str, McpToolSpec] | None = None

    def list_tools(self, *, refresh: bool = False) -> list[McpToolSpec]:
        if self._tool_cache is not None and not refresh:
            return list(self._tool_cache.values())
        raw = self._timed_request("tools/list", {})
        tools_raw = raw.get("tools")
        if not isinstance(tools_raw, list):
            raise McpSchemaError("tools/list 响应缺少 tools[]", detail=str(type(tools_raw)))
        specs: dict[str, McpToolSpec] = {}
        for item in tools_raw:
            if not isinstance(item, dict) or not item.get("name"):
                raise McpSchemaError("tools/list 含非法 tool 项")
            specs[str(item["name"])] = McpToolSpec(
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                input_schema=dict(item.get("inputSchema") or item.get("input_schema") or {}),
            )
        self._tool_cache = specs
        return list(specs.values())

    def get_tool(self, name: str) -> McpToolSpec | None:
        self.list_tools()
        assert self._tool_cache is not None
        return self._tool_cache.get(name)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpCallResult:
        spec = self.get_tool(name)
        if spec is None:
            # 刷新一次再查
            self.list_tools(refresh=True)
            spec = self.get_tool(name)
        if spec is None:
            raise McpSchemaError(f"未知 MCP 工具 '{name}'")
        args = validate_arguments(
            tool_name=name,
            schema_props=spec.properties,
            required=spec.required,
            arguments=arguments,
        )
        raw = self._timed_request(
            "tools/call",
            {"name": name, "arguments": args},
        )
        return self._normalize_call_result(raw)

    def _timed_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            result = self._transport.request(method, params)
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001 — 归一为 unavailable
            raise McpUnavailableError("MCP transport 失败", detail=str(exc)) from exc
        elapsed = time.monotonic() - t0
        if elapsed > self.timeout_s:
            raise McpTimeoutError(
                f"MCP 调用超时 (>{self.timeout_s}s)",
                detail=method,
            )
        if not isinstance(result, dict):
            raise McpSchemaError("MCP 响应非 object", detail=str(type(result)))
        if result.get("error"):
            err = result["error"]
            msg = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
            raise McpUnavailableError(msg)
        return result

    @staticmethod
    def _normalize_call_result(raw: dict[str, Any]) -> McpCallResult:
        is_error = bool(raw.get("isError") or raw.get("is_error"))
        content = raw.get("content")
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
                else:
                    texts.append(json.dumps(block, ensure_ascii=False))
            text = "\n".join(texts)
        elif content is None:
            text = json.dumps(raw.get("result", raw), ensure_ascii=False)
        else:
            text = str(content)
        return McpCallResult(content=text, is_error=is_error, raw=raw)
