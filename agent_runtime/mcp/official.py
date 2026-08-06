"""对接官方 github/github-mcp-server（stdio + PAT）。"""

from __future__ import annotations

import os
import shlex
from typing import Any

from agent_runtime.mcp.client import McpCallResult, McpClient, McpToolSpec
from agent_runtime.mcp.errors import McpError, McpSchemaError, McpUnavailableError
from agent_runtime.mcp.official_map import (
    OFFICIAL_MAP_BY_LOCAL,
    OFFICIAL_TOOL_MAPS,
    adapt_local_call,
)
from agent_runtime.mcp.schema_map import validate_arguments
from agent_runtime.mcp.stdio import StdioTransport

DEFAULT_DOCKER_IMAGE = "ghcr.io/github/github-mcp-server"
DEFAULT_TOOLSETS = "repos,issues,pull_requests,actions"


class OfficialMappedClient:
    """把官方远程工具名映射为 FixLoop ``github_*`` 本地契约。

    对 Registry 暴露与 Mock 相同的 ``list_tools`` / ``call_tool`` / ``server_name``。
    """

    def __init__(self, transport: StdioTransport, *, timeout_s: float = 30.0) -> None:
        self._transport = transport
        self._inner = McpClient(
            transport,
            timeout_s=timeout_s,
            server_name="official-github-mcp",
        )
        self.server_name = self._inner.server_name
        self.timeout_s = timeout_s
        self._remote_names: set[str] | None = None

    def list_tools(self, *, refresh: bool = False) -> list[McpToolSpec]:
        remote = {s.name for s in self._inner.list_tools(refresh=refresh)}
        self._remote_names = remote
        specs: list[McpToolSpec] = []
        for mapping in OFFICIAL_TOOL_MAPS:
            if mapping.remote_name not in remote:
                continue
            specs.append(
                McpToolSpec(
                    name=mapping.local_name,
                    description=mapping.description,
                    input_schema={
                        "type": "object",
                        "properties": mapping.properties,
                        "required": list(mapping.required),
                    },
                )
            )
        return specs

    def get_tool(self, name: str) -> McpToolSpec | None:
        for spec in self.list_tools():
            if spec.name == name:
                return spec
        return None

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpCallResult:
        mapping = OFFICIAL_MAP_BY_LOCAL.get(name)
        if mapping is None:
            raise McpSchemaError(f"未知本地 GitHub MCP 工具 '{name}'")
        args = validate_arguments(
            tool_name=name,
            schema_props=mapping.properties,
            required=mapping.required,
            arguments=arguments,
        )
        # 确保远程已 list，且映射目标存在
        if self._remote_names is None:
            self.list_tools()
        assert self._remote_names is not None
        if mapping.remote_name not in self._remote_names:
            raise McpUnavailableError(
                f"官方 MCP 未提供工具 '{mapping.remote_name}'",
                detail=name,
            )
        remote_name, remote_args = adapt_local_call(name, args)
        raw = self._inner._timed_request(  # noqa: SLF001 — 有意绕过远程 schema 校验
            "tools/call",
            {"name": remote_name, "arguments": remote_args},
        )
        return McpClient._normalize_call_result(raw)

    def close(self) -> None:
        self._transport.close()


def resolve_github_token() -> str | None:
    for key in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_PAT", "FIXLOOP_GITHUB_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return None


def build_official_stdio_command(*, token: str) -> tuple[list[str], dict[str, str]]:
    """构造官方 server 启动命令与额外 env。

    优先级：
    1. ``FIXLOOP_GITHUB_MCP_COMMAND`` — 完整命令行（shell 风格）
    2. Docker 镜像 ``FIXLOOP_GITHUB_MCP_IMAGE``（默认 ghcr.io/github/github-mcp-server）
    """
    toolsets = os.environ.get("FIXLOOP_GITHUB_MCP_TOOLSETS", DEFAULT_TOOLSETS).strip()
    extra_env = {
        "GITHUB_PERSONAL_ACCESS_TOKEN": token,
        "GITHUB_TOOLSETS": toolsets,
    }
    custom = os.environ.get("FIXLOOP_GITHUB_MCP_COMMAND", "").strip()
    if custom:
        return shlex.split(custom, posix=os.name != "nt"), extra_env

    image = os.environ.get("FIXLOOP_GITHUB_MCP_IMAGE", DEFAULT_DOCKER_IMAGE).strip()
    cmd = [
        "docker",
        "run",
        "-i",
        "--rm",
        "-e",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "-e",
        f"GITHUB_TOOLSETS={toolsets}",
        image,
    ]
    return cmd, extra_env


def build_official_github_mcp_client(
    *,
    token: str | None = None,
    timeout_s: float = 60.0,
) -> OfficialMappedClient:
    """启动官方 stdio server 并返回映射客户端。

    Raises:
        McpUnavailableError: 无 token 或进程启动/握手失败。
    """
    tok = (token or resolve_github_token() or "").strip()
    if not tok:
        raise McpUnavailableError(
            "未设置 GITHUB_PERSONAL_ACCESS_TOKEN（或 GITHUB_PAT / FIXLOOP_GITHUB_TOKEN）"
        )
    command, extra_env = build_official_stdio_command(token=tok)
    transport = StdioTransport(command, env=extra_env, timeout_s=timeout_s)
    try:
        transport.start()
    except McpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise McpUnavailableError("官方 GitHub MCP 启动失败", detail=str(exc)) from exc
    return OfficialMappedClient(transport, timeout_s=timeout_s)
