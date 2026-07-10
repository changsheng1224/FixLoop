"""单步工具超时（Gate 9 concurrent.futures）单测。"""

import time

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_executor import ToolExecutor
from agent_runtime.tool_timeout import ToolTimeoutError, run_with_timeout


class TestRunWithTimeout:
    def test_returns_result_when_fast(self):
        assert run_with_timeout(lambda: "ok", timeout_s=5) == "ok"

    def test_raises_when_slow(self):
        with pytest.raises(ToolTimeoutError) as exc:
            run_with_timeout(lambda: time.sleep(2) or "late", timeout_s=1)
        assert exc.value.timeout_s == 1

    def test_disabled_when_zero(self):
        assert run_with_timeout(lambda: time.sleep(0.2) or "ok", timeout_s=0) == "ok"


class TestToolExecutorTimeout:
    @pytest.fixture
    def slow_agent(self, workspace):
        config = AgentConfig(provider="fake", max_steps=4, approval="auto", tool_timeout_s=1)
        client = FakeModelClient(["<final>ok</final>"])
        agent = Agent(config=config, model_client=client, workspace=workspace)
        original_run = agent.tools["list_files"]["run"]
        agent.tools["list_files"]["run"] = lambda args: time.sleep(3) or original_run(args)
        return agent

    def test_gate9_returns_timeout_error(self, slow_agent):
        executor = ToolExecutor(agent=slow_agent, approval_policy="auto")
        t0 = time.time()
        result = executor.execute_gated("list_files", {"path": "."})
        elapsed = time.time() - t0

        assert elapsed < 2.5
        assert "超时" in result.content
        assert result.metadata["tool_status"] == "error"
        assert result.metadata["tool_error_code"] == "tool_timeout"
        assert result.metadata["timeout_s"] == 1
        assert result.metadata["gate_id"] == 9

    def test_disabled_timeout_waits_for_tool(self, workspace):
        config = AgentConfig(provider="fake", approval="auto", tool_timeout_s=0)
        client = FakeModelClient(["<final>ok</final>"])
        agent = Agent(config=config, model_client=client, workspace=workspace)
        agent.tools["list_files"]["run"] = lambda args: time.sleep(0.3) or "[]"
        executor = ToolExecutor(agent=agent, approval_policy="auto")

        t0 = time.time()
        result = executor.execute_gated("list_files", {"path": "."})
        elapsed = time.time() - t0

        assert elapsed >= 0.25
        assert result.metadata.get("tool_status") == "success"
