"""Agentic Loop trace 事件表快照单测（Observe→Context→Model→Tool→Record）。"""

import json
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.loop_trace_schema import (
    LOOP_PATH_SNAPSHOTS,
    assert_subsequence,
    normalize_trace_events,
    validate_loop_trace,
)
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext

_TOOL = '<tool>{"name":"list_files","args":{"path":"."}}</tool>'
_FINAL = "<final>done</final>"


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


def _run_ask(temp_workspace, client):
    config = AgentConfig(provider="fake", max_steps=4)
    ws = WorkspaceContext.build(str(temp_workspace))
    agent = Agent(
        config=config,
        model_client=client,
        workspace=ws,
        cwd=str(temp_workspace),
    )
    agent.ask("list files")
    return _load_events(_latest_trace_file(temp_workspace))


class TestLoopTraceSnapshots:
    def test_xml_one_tool_matches_golden_subsequence(self, temp_workspace):
        events = _run_ask(
            temp_workspace,
            FakeModelClient([_TOOL, _FINAL]),
        )
        names = normalize_trace_events(events)
        golden = LOOP_PATH_SNAPSHOTS["xml"]["one_tool_then_final"]
        assert_subsequence(names, golden)
        errors = validate_loop_trace(events, path="xml")
        assert errors == []

    def test_xml_final_only_matches_golden_subsequence(self, temp_workspace):
        events = _run_ask(temp_workspace, FakeModelClient([_FINAL]))
        names = normalize_trace_events(events)
        golden = LOOP_PATH_SNAPSHOTS["xml"]["final_only"]
        assert_subsequence(names, golden)
        errors = validate_loop_trace(events, path="xml")
        assert errors == []

    def test_native_one_tool_matches_golden_subsequence(self, temp_workspace):
        events = _run_ask(
            temp_workspace,
            FakeNativeToolClient([_TOOL, _FINAL]),
        )
        names = normalize_trace_events(events)
        golden = LOOP_PATH_SNAPSHOTS["native"]["one_tool_then_final"]
        assert_subsequence(names, golden)
        errors = validate_loop_trace(events, path="native")
        assert errors == []

    def test_context_built_before_model_on_both_paths(self, temp_workspace):
        for client, path in (
            (FakeModelClient([_TOOL, _FINAL]), "xml"),
            (FakeNativeToolClient([_TOOL, _FINAL]), "native"),
        ):
            events = _run_ask(temp_workspace, client)
            names = normalize_trace_events(events)
            assert names.index("context_built") < names.index("model_request_start"), path

    def test_tool_executed_after_acting_on_both_paths(self, temp_workspace):
        for client in (FakeModelClient([_TOOL, _FINAL]), FakeNativeToolClient([_TOOL, _FINAL])):
            events = _run_ask(temp_workspace, client)
            names = normalize_trace_events(events)
            assert names.index("react_phase:acting") < names.index("tool_executed")
