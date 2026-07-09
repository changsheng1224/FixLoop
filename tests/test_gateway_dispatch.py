"""ToolGateway.dispatch 与 Agent.execute_tool 单入口测试。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_executor import ToolExecutionResult
from src.agents.localizer import create_localizer


@pytest.fixture
def config():
    return AgentConfig(provider="fake", max_steps=4, approval="auto")


class TestAgentExecuteToolDispatch:
    def test_execute_tool_routes_through_dispatch(self, config, workspace):
        calls: list[tuple[str, str]] = []

        def dispatch(agent_name, tool_name, run_fn):
            calls.append((agent_name, tool_name))
            return run_fn()

        agent = Agent(
            config=config,
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=workspace,
            agent_name="localizer",
            tool_dispatch=dispatch,
        )
        result = agent.execute_tool("list_files", {"path": "."})
        assert calls == [("localizer", "list_files")]
        assert result.metadata["tool_status"] == "success"

    def test_dispatch_denied_skips_executor(self, config, workspace):
        def dispatch(agent_name, tool_name, run_fn):
            del agent_name, run_fn
            return ToolExecutionResult(
                content=f"denied {tool_name}",
                metadata={"tool_status": "rejected", "tool_error_code": "permission_denied"},
            )

        agent = Agent(
            config=config,
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=workspace,
            agent_name="localizer",
            tool_dispatch=dispatch,
        )
        result = agent.execute_tool("write_file", {"path": "x.py", "content": "y"})
        assert result.metadata["tool_error_code"] == "permission_denied"


class TestRepairGatewayDispatch:
    def test_localizer_denied_via_gateway_dispatch(self, workspace):
        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), workspace)
        assert callable(agent._tool_dispatch)
        result = agent.execute_tool("write_file", {"path": "x.py", "content": "y"})
        assert result.metadata["tool_error_code"] == "permission_denied"
