"""Verifier Agent 工厂与 ToolGateway 权限测试。"""

import pytest

from agent_runtime.providers.clients import FakeModelClient
from src.agents.verifier import create_verifier


@pytest.fixture
def client():
    return FakeModelClient(["<final>ok</final>"])


class TestVerifierAgent:
    def test_factory_registers_sandbox_tools(self, client, workspace):
        agent = create_verifier(client, workspace)
        assert "sandbox_build" in agent.tools
        assert "sandbox_test" in agent.tools
        assert "sandbox_verify" in agent.tools

    def test_verifier_blocked_from_write(self, client, workspace):
        agent = create_verifier(client, workspace)
        result = agent.execute_tool("write_file", {"path": "x.py", "content": "y"})
        assert result.metadata["tool_error_code"] == "permission_denied"

    def test_verifier_blocked_from_ast_parse(self, client, workspace):
        agent = create_verifier(client, workspace)
        result = agent.execute_tool("ast_parse", {"path": "x.py"})
        assert result.metadata["tool_error_code"] == "permission_denied"

    def test_verifier_ask_returns_answer(self, client, workspace):
        agent = create_verifier(client, workspace)
        answer = agent.ask("验证补丁")
        assert "ok" in answer
