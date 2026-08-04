"""将 MCP tools 映射为 FixLoop 工具注册表条目。"""

from __future__ import annotations

import os
from typing import Any, Protocol

from agent_runtime.mcp.client import InProcessTransport, McpClient
from agent_runtime.mcp.errors import McpError, McpUnavailableError
from agent_runtime.mcp.github_allowlist import (
    GITHUB_MCP_READ_TOOLS,
    GITHUB_MCP_WRITE_TOOLS,
    is_github_mcp_tool_allowed,
)
from agent_runtime.mcp.mock_server import MockGitHubMcpServer
from agent_runtime.mcp.schema_map import json_schema_to_fixloop


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


def build_github_mcp_tool_registry(client: _McpClientLike) -> dict[str, dict[str, Any]]:
    """``tools/list`` → 过滤 allowlist → FixLoop tools dict。

    每条含 ``schema/risky/execution_tier/description/run``，
    ``run`` 内部 ``tools/call`` 并把结果写成 Observation 字符串。
    """
    tools: dict[str, dict[str, Any]] = {}
    for spec in client.list_tools():
        if not is_github_mcp_tool_allowed(spec.name):
            continue
        schema = json_schema_to_fixloop(spec.properties, spec.required)
        risky = spec.name in GITHUB_MCP_WRITE_TOOLS
        tools[spec.name] = {
            "schema": schema,
            "risky": risky,
            "execution_tier": "host",
            "description": spec.description or f"GitHub MCP: {spec.name}",
            "mcp_server": client.server_name,
            "mcp_tool": spec.name,
            "run": _make_runner(client, spec.name),
        }
    return tools


def _make_runner(client: _McpClientLike, tool_name: str):
    def run(args: dict) -> str:
        try:
            result = client.call_tool(tool_name, args)
            return result.observation()
        except McpError as exc:
            return exc.observation()

    return run


def github_mcp_permission_grants() -> dict[str, set[str]]:
    """返回可并入 ``REPAIR_PERMISSION_TABLE`` 的权限片段。

    读工具：localizer/retriever/patcher；写 draft_pr：仅 patcher（且须 ask）。
    """
    table: dict[str, set[str]] = {}
    readers = {"localizer", "retriever", "patcher"}
    for name in GITHUB_MCP_READ_TOOLS:
        table[name] = set(readers)
    for name in GITHUB_MCP_WRITE_TOOLS:
        table[name] = {"patcher"}
    return table


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
        client, _ = build_mock_github_mcp_client()
        return build_github_mcp_tool_registry(client)
