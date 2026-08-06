"""GitHub MCP 最小闭环：Client / Allowlist / Registry / Gateway / Draft PR ask。"""

from __future__ import annotations

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.mcp import (
    GITHUB_MCP_DENIED_TOOLS,
    GITHUB_MCP_READ_TOOLS,
    GITHUB_MCP_WRITE_TOOLS,
    McpSchemaError,
    McpTimeoutError,
    McpUnavailableError,
    MockGitHubMcpServer,
    build_github_mcp_tool_registry,
    is_github_mcp_tool_allowed,
)
from agent_runtime.mcp.client import InProcessTransport, McpClient
from agent_runtime.mcp.registry import build_mock_github_mcp_client
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_executor import ToolExecutor
from agent_runtime.tools import build_tool_registry
from src.middleware import build_repair_gateway


@pytest.fixture
def mock_pair():
    return build_mock_github_mcp_client()


class TestAllowlist:
    def test_read_and_write_allowed(self):
        for name in GITHUB_MCP_READ_TOOLS | GITHUB_MCP_WRITE_TOOLS:
            assert is_github_mcp_tool_allowed(name)

    def test_dangerous_denied(self):
        for name in (
            "github_merge_pull_request",
            "github_delete_branch",
            "github_update_repo_secrets",
        ):
            assert name in GITHUB_MCP_DENIED_TOOLS
            assert not is_github_mcp_tool_allowed(name)


class TestMcpClientHappyPath:
    def test_list_and_call_issue(self, mock_pair):
        client, server = mock_pair
        specs = client.list_tools()
        names = {s.name for s in specs}
        assert "github_get_issue" in names
        assert "github_merge_pull_request" in names  # server 仍 list，Registry 再滤

        result = client.call_tool(
            "github_get_issue",
            {"owner": "acme", "repo": "demo", "number": 1},
        )
        assert not result.is_error
        assert "TypeError" in result.observation()
        assert server.call_log[-1][0] == "github_get_issue"

    def test_create_draft_pr(self, mock_pair):
        client, _server = mock_pair
        result = client.call_tool(
            "github_create_draft_pr",
            {
                "owner": "acme",
                "repo": "demo",
                "title": "Fix TypeError",
                "head": "fix/typeerror",
            },
        )
        obs = result.observation()
        assert "draft" in obs
        assert "Fix TypeError" in obs


class TestMcpErrors:
    def test_timeout(self):
        client, _server = build_mock_github_mcp_client(timeout_s=0.05, call_delay_s=0.2)
        with pytest.raises(McpTimeoutError) as ei:
            client.call_tool("github_get_repo", {"owner": "a", "repo": "b"})
        assert "mcp_timeout" in ei.value.observation()

    def test_unavailable(self):
        client, _server = build_mock_github_mcp_client(fail_mode="unavailable")
        with pytest.raises(McpUnavailableError) as ei:
            client.list_tools()
        assert "mcp_unavailable" in ei.value.observation()

    def test_schema_missing_required(self, mock_pair):
        client, _server = mock_pair
        with pytest.raises(McpSchemaError) as ei:
            client.call_tool("github_get_issue", {"owner": "acme", "repo": "demo"})
        assert "mcp_schema_error" in ei.value.observation()
        assert "number" in ei.value.detail

    def test_schema_unknown_arg(self, mock_pair):
        client, _server = mock_pair
        with pytest.raises(McpSchemaError) as ei:
            client.call_tool(
                "github_get_repo",
                {"owner": "acme", "repo": "demo", "extra": "nope"},
            )
        assert "extra" in ei.value.detail

    def test_transport_down(self):
        server = MockGitHubMcpServer()
        transport = InProcessTransport(server.handle)
        transport.available = False
        client = McpClient(transport, timeout_s=1.0)
        with pytest.raises(McpUnavailableError):
            client.list_tools()


class TestRegistry:
    def test_denied_tools_not_registered(self, mock_pair):
        client, _server = mock_pair
        tools = build_github_mcp_tool_registry(client)
        for name in GITHUB_MCP_DENIED_TOOLS:
            assert name not in tools
        assert set(GITHUB_MCP_READ_TOOLS).issubset(tools)
        assert set(GITHUB_MCP_WRITE_TOOLS).issubset(tools)
        assert tools["github_create_draft_pr"]["risky"] is True
        assert tools["github_list_issues"]["risky"] is False

    def test_run_returns_observation_string(self, mock_pair):
        client, _server = mock_pair
        tools = build_github_mcp_tool_registry(client)
        out = tools["github_list_issues"]["run"]({"owner": "acme", "repo": "demo"})
        assert isinstance(out, str)
        assert "TypeError" in out

    def test_run_normalizes_errors(self):
        client, _ = build_mock_github_mcp_client(timeout_s=0.05, call_delay_s=0.2)
        tools = build_github_mcp_tool_registry(client)
        out = tools["github_get_repo"]["run"]({"owner": "a", "repo": "b"})
        assert out.startswith("Error:")
        assert "mcp_timeout" in out


class TestGatewayPermissions:
    def test_mcp_specs_bind_into_gateway(self, mock_pair):
        client, _ = mock_pair
        tools = build_github_mcp_tool_registry(client)
        gw = build_repair_gateway()
        gw.bind_tools(tools)
        assert gw.can_call("patcher", "github_create_draft_pr") is True
        assert gw.can_call("verifier", "github_create_draft_pr") is False
        assert gw.can_call("verifier", "github_list_issues") is True

    def test_gateway_rejects_verifier_draft_pr(self, workspace):
        from agent_runtime.tool_context import ToolContext

        client, _ = build_mock_github_mcp_client()
        mcp_tools = build_github_mcp_tool_registry(client)
        tools = {**build_tool_registry(ToolContext(root=workspace.repo_root)), **mcp_tools}
        gw = build_repair_gateway()
        gw.bind_tools(tools)
        agent = Agent(
            config=AgentConfig(provider="fake", approval="auto"),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=workspace,
            tools=tools,
            agent_name="verifier",
            tool_dispatch=gw.dispatch,
        )
        result = agent.execute_tool(
            "github_create_draft_pr",
            {
                "owner": "acme",
                "repo": "demo",
                "title": "x",
                "head": "fix/x",
            },
        )
        assert result.metadata.get("tool_error_code") == "permission_denied"
        assert result.metadata.get("rejection_layer") == "gateway"


class TestDraftPrAskApproval:
    def test_draft_pr_is_ask_tier(self):
        assert ToolExecutor._approval_tier("github_create_draft_pr") == ToolExecutor._APPROVAL_TIER_ASK

    def test_read_mcp_tools_are_auto(self):
        for name in GITHUB_MCP_READ_TOOLS:
            assert ToolExecutor._approval_tier(name) == ToolExecutor._APPROVAL_TIER_AUTO

    def test_draft_pr_asks_under_ask_policy(self, workspace):
        from agent_runtime.tool_context import ToolContext

        client, _ = build_mock_github_mcp_client()
        mcp_tools = build_github_mcp_tool_registry(client)
        tools = {**build_tool_registry(ToolContext(root=workspace.repo_root)), **mcp_tools}
        agent = Agent(
            config=AgentConfig(provider="fake", approval="ask"),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=workspace,
            tools=tools,
        )
        exe = ToolExecutor(agent, approval_policy="ask")
        result = exe.execute_gated(
            "github_create_draft_pr",
            {
                "owner": "acme",
                "repo": "demo",
                "title": "Fix",
                "head": "fix/x",
            },
        )
        assert result.metadata.get("tool_status") == "rejected"
        assert result.metadata.get("gate_id") == 7

    def test_draft_pr_runs_under_auto_policy(self, workspace):
        from agent_runtime.tool_context import ToolContext

        client, server = build_mock_github_mcp_client()
        mcp_tools = build_github_mcp_tool_registry(client)
        tools = {**build_tool_registry(ToolContext(root=workspace.repo_root)), **mcp_tools}
        agent = Agent(
            config=AgentConfig(provider="fake", approval="auto"),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=workspace,
            tools=tools,
        )
        exe = ToolExecutor(agent, approval_policy="auto")
        result = exe.execute_gated(
            "github_create_draft_pr",
            {
                "owner": "acme",
                "repo": "demo",
                "title": "Fix",
                "head": "fix/x",
            },
        )
        assert result.metadata.get("tool_status") == "success"
        assert "Fix" in result.content
        assert any(n == "github_create_draft_pr" for n, _ in server.call_log)


class TestCompositeOptIn:
    def test_env_enables_mcp_tools(self, workspace, monkeypatch):
        from agent_runtime.tool_context import ToolContext
        from src.tools.composite import build_repair_agent_tools, build_repair_canonical_tools

        monkeypatch.setenv("FIXLOOP_ENABLE_GITHUB_MCP", "1")
        monkeypatch.setenv("FIXLOOP_GITHUB_MCP_MODE", "mock")
        ctx = ToolContext(root=workspace.repo_root)
        tools = build_repair_agent_tools(ctx, "patcher")
        assert "github_list_issues" in tools
        assert "github_create_draft_pr" in tools
        # canonical 仍不含 MCP，避免破坏 schema-sync
        canonical = build_repair_canonical_tools(ctx)
        assert "github_list_issues" not in canonical

    def test_default_off(self, workspace, monkeypatch):
        from agent_runtime.tool_context import ToolContext
        from src.tools.composite import build_repair_agent_tools

        monkeypatch.delenv("FIXLOOP_ENABLE_GITHUB_MCP", raising=False)
        tools = build_repair_agent_tools(ToolContext(root=workspace.repo_root), "patcher")
        assert "github_list_issues" not in tools
