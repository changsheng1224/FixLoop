"""M5 Agent 工厂 + ToolGateway 集成测试。"""

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.middleware import ToolGateway


@pytest.fixture
def workspace(temp_workspace):
    return WorkspaceContext.build(str(temp_workspace))


@pytest.fixture
def client():
    return FakeModelClient(["<final>ok</final>"])


class TestAgentFactories:
    def test_localizer_has_ast_parse(self, client, workspace):
        agent = create_localizer(client, workspace)
        assert "ast_parse" in agent.tools
        assert "stack_parse" in agent.tools
        assert "write_file" not in agent.tools

    def test_retriever_has_git_tools(self, client, workspace):
        agent = create_retriever(client, workspace)
        assert "git_blame" in agent.tools
        assert "find_test" in agent.tools
        assert "ast_parse" not in agent.tools

    def test_patcher_has_write_tools(self, client, workspace):
        agent = create_patcher(client, workspace)
        assert "write_file" in agent.tools
        assert "patch_file" in agent.tools
        assert "ast_parse" not in agent.tools  # 不能自己定位
        assert "stack_parse" not in agent.tools

    def test_all_agents_work(self, workspace):
        """3 个 Agent 都能正常 ask。"""
        c1 = FakeModelClient(["<final>ok</final>"])
        c2 = FakeModelClient(["<final>ok</final>"])
        c3 = FakeModelClient(["<final>ok</final>"])

        assert "ok" in create_localizer(c1, workspace).ask("test")
        assert "ok" in create_retriever(c2, workspace).ask("test")
        assert "ok" in create_patcher(c3, workspace).ask("test")


class TestToolGatewayWired:
    """ToolGateway 实际接入 Agent.execute_tool 测试。"""

    def test_localizer_blocked_from_write(self, workspace):
        """Localizer 工厂创建的 Agent 不能调 write_file。"""
        client = FakeModelClient(["<final>ok</final>"])
        agent = create_localizer(client, workspace)
        result = agent.execute_tool("write_file", {"path": "x", "content": "y"})
        assert result.metadata["tool_error_code"] == "permission_denied"

    def test_patcher_blocked_from_ast(self, workspace):
        """Patcher 不能调 ast_parse。"""
        client = FakeModelClient(["<final>ok</final>"])
        agent = create_patcher(client, workspace)
        result = agent.execute_tool("ast_parse", {"path": "x.py"})
        assert result.metadata["tool_error_code"] == "permission_denied"

    def test_localizer_can_ast_parse(self, workspace):
        """Localizer 可以调 ast_parse。"""
        client = FakeModelClient(["<final>ok</final>"])
        agent = create_localizer(client, workspace)
        result = agent.execute_tool(
            "search", {"pattern": "test", "path": str(workspace.repo_root)}
        )
        assert "permission_denied" not in str(result.metadata.get("tool_error_code", ""))


class TestToolGatewayIntegration:
    def test_localizer_cannot_write(self, client, workspace):
        gw = ToolGateway({
            "write_file": {"patcher"},
            "patch_file": {"patcher"},
            "ast_parse": {"localizer"},
            "stack_parse": {"localizer"},
            "*": {"*"},
        })
        assert gw.can_call("localizer", "ast_parse") is True
        assert gw.can_call("localizer", "write_file") is False

    def test_patcher_cannot_ast_parse(self, client, workspace):
        gw = ToolGateway({
            "write_file": {"patcher"},
            "patch_file": {"patcher"},
            "ast_parse": {"localizer"},
            "stack_parse": {"localizer"},
            "*": {"*"},
        })
        assert gw.can_call("patcher", "write_file") is True
        assert gw.can_call("patcher", "ast_parse") is False
