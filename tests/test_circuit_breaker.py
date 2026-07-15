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

    def test_half_open_recovers_after_two_successes(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert cb.state == "open"

        time.sleep(0.1)
        assert cb.call(lambda: "probe-1") == "probe-1"
        assert cb.state == "half_open"
        assert cb.half_open_success_count == 1

        assert cb.call(lambda: "probe-2") == "probe-2"
        assert cb.state == "closed"
        assert cb.half_open_success_count == 0

    def test_half_open_one_success_stays_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        time.sleep(0.1)
        cb.call(lambda: "ok")
        assert cb.state == "half_open"
        assert cb.half_open_success_count == 1

    def test_half_open_failure_reopens_immediately(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=0.05)
        for _ in range(5):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        time.sleep(0.1)
        cb.call(lambda: "probe-ok")
        assert cb.state == "half_open"
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail again")))
        assert cb.state == "open"
        assert cb.half_open_success_count == 0

    def test_half_open_fails_back_to_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        time.sleep(0.1)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail again")))
        assert cb.state == "open"

    def test_rate_limit_exhausted_does_not_trip_breaker(self):
        from agent_runtime.providers.retry_policy import RateLimitExceededError

        cb = CircuitBreaker(failure_threshold=2)
        for _ in range(2):
            with pytest.raises(RateLimitExceededError):
                cb.call(lambda: (_ for _ in ()).throw(RateLimitExceededError("429 exhausted")))
        assert cb.state == "closed"
        assert cb.call(lambda: "ok") == "ok"


class TestCircuitBreakerTraceEvents:
    """熔断状态迁移 listener 事件。"""

    def test_emits_circuit_opened_on_threshold(self):
        events: list[tuple[str, dict]] = []
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30)
        cb.add_listener(lambda event, payload: events.append((event, payload)))

        def fail():
            return (_ for _ in ()).throw(RuntimeError("fail"))

        with pytest.raises(RuntimeError):
            cb.call(fail)
        assert not events

        with pytest.raises(RuntimeError):
            cb.call(fail)
        assert events[-1][0] == "circuit_opened"
        assert events[-1][1]["reason"] == "consecutive_failures"
        assert events[-1][1]["failure_count"] == 2

    def test_emits_half_open_probe_and_circuit_closed(self):
        events: list[tuple[str, dict]] = []
        cb = CircuitBreaker(
            failure_threshold=2, recovery_timeout=0.05, half_open_success_threshold=2
        )
        cb.add_listener(lambda event, payload: events.append((event, payload)))

        def fail():
            return (_ for _ in ()).throw(RuntimeError("fail"))

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        time.sleep(0.1)
        cb.call(lambda: "probe-1")
        half_open = [p for e, p in events if e == "half_open_probe"]
        assert len(half_open) == 1
        assert half_open[0]["half_open_success_threshold"] == 2

        cb.call(lambda: "probe-2")
        closed = [p for e, p in events if e == "circuit_closed"]
        assert len(closed) == 1
        assert closed[0]["probes_required"] == 2
        assert cb.state == "closed"

    def test_half_open_failure_reopens_with_reason(self):
        events: list[tuple[str, dict]] = []
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        cb.add_listener(lambda event, payload: events.append((event, payload)))

        def fail():
            return (_ for _ in ()).throw(RuntimeError("fail"))

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(fail)
        time.sleep(0.1)
        cb.call(lambda: "probe")
        with pytest.raises(RuntimeError):
            cb.call(fail)

        opened = [p for e, p in events if e == "circuit_opened"]
        assert len(opened) == 2
        assert opened[-1]["reason"] == "half_open_probe_failed"


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
