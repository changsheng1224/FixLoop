"""单步 wall-clock 超时单测。"""

import json
import time
from pathlib import Path

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.step_clock import StepClock, StepTimeoutError
from agent_runtime.workspace import WorkspaceContext

_TOOL = '<tool>{"name":"list_files","args":{"path":"."}}</tool>'


def _latest_report(workspace: Path) -> dict:
    runs = sorted((workspace / ".agent" / "runs").iterdir(), key=lambda p: p.stat().st_mtime)
    return json.loads((runs[-1] / "report.json").read_text(encoding="utf-8"))


def _latest_events(workspace: Path) -> list[str]:
    runs = sorted((workspace / ".agent" / "runs").iterdir(), key=lambda p: p.stat().st_mtime)
    return [
        json.loads(line)["event"]
        for line in (runs[-1] / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]


class TestStepClock:
    def test_disabled_never_expires(self):
        clock = StepClock(0)
        time.sleep(0.05)
        clock.check(step=1)

    def test_raises_when_expired(self):
        clock = StepClock(1)
        time.sleep(1.05)
        with pytest.raises(StepTimeoutError) as exc:
            clock.check(step=2, path="xml")
        assert exc.value.timeout_s == 1
        assert exc.value.step == 2
        assert exc.value.path == "xml"


class TestXmlStepTimeout:
    def test_stops_run_when_model_phase_exceeds_limit(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=4, step_timeout_s=1, tool_timeout_s=0)
        client = FakeModelClient(["<final>done</final>"])
        client.complete = lambda *a, **k: (time.sleep(2), "<final>done</final>")[1]  # type: ignore[method-assign]
        agent = Agent(
            config=config,
            model_client=client,
            workspace=WorkspaceContext.build(str(temp_workspace)),
            cwd=str(temp_workspace),
        )
        answer = agent.ask("hello")

        assert "超时" in answer
        assert _latest_report(temp_workspace)["stop_reason"] == "step_timeout"

    def test_trace_emits_step_timeout(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=4, step_timeout_s=1, tool_timeout_s=0)
        client = FakeModelClient(["<final>done</final>"])
        client.complete = lambda *a, **k: (time.sleep(2), "<final>done</final>")[1]  # type: ignore[method-assign]
        agent = Agent(
            config=config,
            model_client=client,
            workspace=WorkspaceContext.build(str(temp_workspace)),
            cwd=str(temp_workspace),
        )
        agent.ask("hello")

        events = _latest_events(temp_workspace)
        assert "step_timeout" in events
        assert "run_finished" in events


class TestNativeStepTimeout:
    def test_stops_on_slow_turn(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=4, step_timeout_s=1, tool_timeout_s=0)
        client = FakeNativeToolClient(["<final>done</final>"])
        client.complete = lambda *a, **k: (time.sleep(2), "<final>done</final>")[1]  # type: ignore[method-assign]
        agent = Agent(
            config=config,
            model_client=client,
            workspace=WorkspaceContext.build(str(temp_workspace)),
            cwd=str(temp_workspace),
        )
        answer = agent.ask("hello")

        assert "超时" in answer
        assert _latest_report(temp_workspace)["stop_reason"] == "step_timeout"


class TestStepTimeoutBeforeTool:
    def test_xml_aborts_before_slow_tool(self, workspace, temp_workspace):
        config = AgentConfig(
            provider="fake",
            max_steps=4,
            step_timeout_s=1,
            tool_timeout_s=0,
            approval="auto",
        )
        client = FakeModelClient([_TOOL, "<final>done</final>"])
        client.complete = lambda *a, **k: (time.sleep(2), _TOOL)[1]  # type: ignore[method-assign]
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            cwd=str(temp_workspace),
        )
        agent.tools["list_files"]["run"] = lambda args: time.sleep(3) or "[]"

        t0 = time.time()
        answer = agent.ask("list")
        elapsed = time.time() - t0

        assert "超时" in answer
        assert elapsed < 2.5
        assert _latest_report(temp_workspace)["stop_reason"] == "step_timeout"
        assert "tool_executed" not in _latest_events(temp_workspace)
