"""将 MCP tools 映射为 FixLoop 工具注册表条目。"""

from __future__ import annotations

import os
import time
from typing import Any, Protocol

from agent_runtime.mcp.client import InProcessTransport, McpClient
from agent_runtime.mcp.errors import McpError, McpUnavailableError
from agent_runtime.mcp.github_allowlist import (
    GITHUB_MCP_WRITE_TOOLS,
    is_github_mcp_tool_allowed,
)
from agent_runtime.mcp.mock_server import MockGitHubMcpServer
from agent_runtime.mcp.schema_map import json_schema_to_fixloop
from src.tools.spec import ToolSpec, project_tool_specs


class _McpClientLike(Protocol):
    server_name: str

    def list_tools(self, *, refresh: bool = False) -> list: ...

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None): ...


def build_mock_github_mcp_client(
    *,
    timeout_s: float = 10.0,
    fail_mode: str | None = None,
    call_delay_s: float = 0.0,
) -> tuple[McpClient, MockGitHubMcpServer]:
    """构造 Mock 客户端（测试 / 本地演示）。"""
    server = MockGitHubMcpServer(fail_mode=fail_mode, call_delay_s=call_delay_s)
    transport = InProcessTransport(server.handle)
    client = McpClient(transport, timeout_s=timeout_s, server_name="mock-github-mcp")
    return client, server


class McpToolExecution(str):
    """Canonical result passed from an MCP adapter to ToolExecutor."""

    def __new__(
        cls,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        structured_facts: list[dict[str, Any]] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> McpToolExecution:
        value = super().__new__(cls, content)
        value.content = content
        value.metadata = dict(metadata or {})
        value.structured_facts = list(structured_facts or [])
        value.raw = dict(raw or {})
        return value


def build_github_mcp_tool_specs(client: _McpClientLike) -> list[ToolSpec]:
    """Discover allowed GitHub tools and build canonical ToolSpecs."""
    specs: list[ToolSpec] = []
    for remote in client.list_tools():
        if not is_github_mcp_tool_allowed(remote.name):
            continue
        is_write = remote.name in GITHUB_MCP_WRITE_TOOLS
        specs.append(
            ToolSpec(
                name=remote.name,
                description=remote.description or f"GitHub MCP: {remote.name}",
                input_schema=json_schema_to_fixloop(remote.properties, remote.required),
                protocol_schema={
                    "type": "object",
                    "properties": dict(remote.properties),
                    "required": list(remote.required),
                    "additionalProperties": False,
                },
                executor=_make_runner(client, remote.name),
                roles=frozenset({"patcher"} if is_write else {"*"}),
                phases=frozenset({"context", "patch", "verify", "verification"}),
                budget_group="write" if is_write else "read",
                timeout_s=float(getattr(client, "timeout_s", 30.0) or 30.0),
                side_effect="external_write" if is_write else "read",
                replay_policy="never_replay" if is_write else "revalidate",
                trust_level="external",
                capabilities=frozenset(
                    {"github.write", "mcp.tools.call"}
                    if is_write
                    else {"github.read", "mcp.tools.call"}
                ),
                provider="mcp",
                server=client.server_name,
            )
        )
    return specs


def build_github_mcp_tool_registry(client: _McpClientLike) -> dict[str, dict[str, Any]]:
    """``tools/list`` → 过滤 allowlist → FixLoop tools dict。

    每条含 ``schema/risky/execution_tier/description/run``，
    ``run`` 内部 ``tools/call`` 并把结果写成 Observation 字符串。
    """
    tools = project_tool_specs(build_github_mcp_tool_specs(client))
    for name, tool in tools.items():
        tool["mcp_server"] = client.server_name
        tool["mcp_tool"] = name
    return tools


def _make_runner(client: _McpClientLike, tool_name: str):
    def run(args: dict) -> McpToolExecution:
        started = time.monotonic()
        try:
            result = client.call_tool(tool_name, args)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            status = "error" if result.is_error else "success"
            return McpToolExecution(
                content=result.observation(),
                metadata={
                    "tool_status": status,
                    "tool_error_code": "tool_execution_failed" if result.is_error else "",
                    "retryable": False,
                    "provider": "mcp",
                    "mcp_server": client.server_name,
                    "mcp_tool": tool_name,
                    "mcp_duration_ms": elapsed_ms,
                },
                structured_facts=result.structured_facts(),
                raw=result.raw,
            )
        except McpError as exc:
            metadata = exc.metadata()
            metadata.update(
                {
                    "provider": "mcp",
                    "mcp_server": client.server_name,
                    "mcp_tool": tool_name,
                    "mcp_duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return McpToolExecution(content=exc.observation(), metadata=metadata)

    return run


def _want_official() -> bool:
    mode = os.environ.get("FIXLOOP_GITHUB_MCP_MODE", "").strip().lower()
    if mode in ("mock", "fake"):
        return False
    if mode in ("official", "real", "stdio"):
        return True
    # auto：有 token 则官方
    from agent_runtime.mcp.official import resolve_github_token

    return resolve_github_token() is not None


def open_github_mcp_client(
    *,
    timeout_s: float | None = None,
) -> tuple[_McpClientLike, Any]:
    """打开 GitHub MCP 客户端。

    Returns:
        (client, handle) — handle 为 MockServer 或 OfficialMappedClient（可 close）。
        官方模式：``FIXLOOP_GITHUB_MCP_MODE=official`` 或 auto+token。
        否则 Mock。
    """
    if _want_official():
        from agent_runtime.mcp.official import build_official_github_mcp_client

        to = 60.0 if timeout_s is None else timeout_s
        client = build_official_github_mcp_client(timeout_s=to)
        return client, client
    to = 10.0 if timeout_s is None else timeout_s
    client, server = build_mock_github_mcp_client(timeout_s=to)
    return client, server


def build_github_mcp_tools_auto() -> dict[str, dict[str, Any]]:
    """按环境自动选 Mock/官方并构建工具表。

    官方启动失败时回退 Mock（避免修复流水线因 MCP 挂掉）。
    """
    try:
        client, _handle = open_github_mcp_client()
        return build_github_mcp_tool_registry(client)
    except McpUnavailableError:
        fallback = os.environ.get("FIXLOOP_GITHUB_MCP_FALLBACK", "error").strip().lower()
        if fallback in {"mock", "fake"}:
            client, _ = build_mock_github_mcp_client()
            return build_github_mcp_tool_registry(client)
        raise
