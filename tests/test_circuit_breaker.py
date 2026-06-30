"""CircuitBreaker + Replay 单测。"""

import time

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
)
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.replay import ReplayResult, ReplayRunner
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


class TestCircuitBreaker:
    """CircuitBreaker 状态机测试。"""

    def test_initial_state_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == "closed"

    def test_normal_call_passes_through(self):
        cb = CircuitBreaker()
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == "closed"

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        for i in range(3):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert cb.state == "open"

    def test_rejects_when_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=999)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        with pytest.raises(CircuitBreakerOpenError):
            cb.call(lambda: "ok")

    def test_half_open_recovers(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert cb.state == "open"

        # 等待恢复
        time.sleep(0.1)
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == "closed"

    def test_half_open_fails_back_to_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        time.sleep(0.1)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail again")))
        assert cb.state == "open"


class TestReplay:
    """ReplayRunner 测试。"""

    def test_empty_trace_no_errors(self, temp_workspace):
        trace_path = temp_workspace / "trace.jsonl"
        trace_path.write_text("")

        config = AgentConfig(provider="fake")
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        agent = Agent(config=config, model_client=client, workspace=ws)

        runner = ReplayRunner(str(trace_path))
        result = runner.replay(agent)
        assert result.total == 0

    def test_replay_result_defaults(self):
        r = ReplayResult()
        assert r.all_match is True
        assert r.total == 0

    def test_trace_file_not_found(self, temp_workspace):
        config = AgentConfig(provider="fake")
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        agent = Agent(config=config, model_client=client, workspace=ws)

        runner = ReplayRunner("/nonexistent/trace.jsonl")
        result = runner.replay(agent)
        assert len(result.errors) == 1
