"""ReAct 阶段 trace 事件序列单测。"""

import json
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.react_phases import ReactPhase
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def _latest_trace_file(workspace: Path) -> Path:
    runs_dir = workspace / ".agent" / "runs"
    run_dirs = sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    return run_dirs[-1] / "trace.jsonl"


def _load_events(trace_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    ]


def _event_indices(events: list[dict], name: str) -> list[int]:
    return [i for i, row in enumerate(events) if row.get("event") == name]


def _react_phases(events: list[dict]) -> list[str]:
    return [
        row["payload"]["phase"]
        for row in events
        if row.get("event") == "react_phase"
    ]


class TestXmlReactTrace:
    def test_tool_step_phase_sequence(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=4)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>done</final>",
            ]
        )
        agent = Agent(
            config=config,
            model_client=client,
            workspace=ws,
            cwd=str(temp_workspace),
        )
        agent.ask("list files")

        events = _load_events(_latest_trace_file(temp_workspace))
        phases = _react_phases(events)
        assert ReactPhase.REASONING in phases
        assert ReactPhase.ACTING in phases
        assert ReactPhase.OBSERVATION in phases
        assert ReactPhase.RECORDING in phases
        assert phases.index(ReactPhase.REASONING) < phases.index(ReactPhase.ACTING)
        assert phases.index(ReactPhase.ACTING) < phases.index(ReactPhase.OBSERVATION)
        assert phases.index(ReactPhase.OBSERVATION) < phases.index(ReactPhase.RECORDING)

        acting = next(
            row for row in events if row.get("event") == "react_phase" and row["payload"]["phase"] == "acting"
        )
        assert acting["payload"]["path"] == "xml"
        assert acting["payload"]["tool"] == "list_files"

    def test_context_built_before_reasoning(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=4)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>done</final>",
            ]
        )
        agent = Agent(
            config=config,
            model_client=client,
            workspace=ws,
            cwd=str(temp_workspace),
        )
        agent.ask("list files")

        events = _load_events(_latest_trace_file(temp_workspace))
        ctx_idx = _event_indices(events, "context_built")[0]
        reasoning_idx = next(
            i
            for i, row in enumerate(events)
            if row.get("event") == "react_phase" and row["payload"]["phase"] == "reasoning"
        )
        assert ctx_idx < reasoning_idx

    def test_tool_executed_after_acting(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=4)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>done</final>",
            ]
        )
        agent = Agent(
            config=config,
            model_client=client,
            workspace=ws,
            cwd=str(temp_workspace),
        )
        agent.ask("list files")

        events = _load_events(_latest_trace_file(temp_workspace))
        acting_idx = next(
            i
            for i, row in enumerate(events)
            if row.get("event") == "react_phase" and row["payload"]["phase"] == "acting"
        )
        tool_idx = _event_indices(events, "tool_executed")[0]
        assert acting_idx < tool_idx


class TestNativeReactTrace:
    def test_native_path_phase_sequence(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=4)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeNativeToolClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>done</final>",
            ]
        )
        agent = Agent(
            config=config,
            model_client=client,
            workspace=ws,
            cwd=str(temp_workspace),
        )
        agent.ask("list files")

        events = _load_events(_latest_trace_file(temp_workspace))
        phases = _react_phases(events)
        assert ReactPhase.REASONING in phases
        assert ReactPhase.ACTING in phases
        assert ReactPhase.OBSERVATION in phases
        assert ReactPhase.RECORDING in phases

        native = next(row for row in events if row.get("event") == "react_phase")
        assert native["payload"]["path"] == "native"
