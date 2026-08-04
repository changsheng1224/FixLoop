"""GitHub MCP 最小闭环：Client / Mock / Official stdio / Allowlist / Registry。

协议子集：``tools/list``、``tools/call``。
- Mock：进程内 transport
- Official：stdio 对接 ``ghcr.io/github/github-mcp-server``（PAT）

产品/实现链路说明见 ``docs/GITHUB_MCP.md``。
"""

from agent_runtime.mcp.client import McpClient, McpToolSpec
from agent_runtime.mcp.errors import (
    McpError,
    McpSchemaError,
    McpTimeoutError,
    McpUnavailableError,
)
from agent_runtime.mcp.github_allowlist import (
    GITHUB_MCP_DENIED_TOOLS,
    GITHUB_MCP_READ_TOOLS,
    GITHUB_MCP_WRITE_TOOLS,
    is_github_mcp_tool_allowed,
)
from agent_runtime.mcp.mock_server import MockGitHubMcpServer
from agent_runtime.mcp.official import (
    OfficialMappedClient,
    build_official_github_mcp_client,
    resolve_github_token,
)
from agent_runtime.mcp.registry import (
    build_github_mcp_tool_registry,
    build_github_mcp_tools_auto,
    build_mock_github_mcp_client,
    open_github_mcp_client,
)
from agent_runtime.mcp.stdio import StdioTransport

__all__ = [
    "McpClient",
    "McpToolSpec",
    "McpError",
    "McpSchemaError",
    "McpTimeoutError",
    "McpUnavailableError",
    "MockGitHubMcpServer",
    "StdioTransport",
    "OfficialMappedClient",
    "GITHUB_MCP_READ_TOOLS",
    "GITHUB_MCP_WRITE_TOOLS",
    "GITHUB_MCP_DENIED_TOOLS",
    "is_github_mcp_tool_allowed",
    "build_github_mcp_tool_registry",
    "build_mock_github_mcp_client",
    "build_official_github_mcp_client",
    "build_github_mcp_tools_auto",
    "open_github_mcp_client",
    "resolve_github_token",
]
