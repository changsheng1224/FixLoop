"""stop_reason 枚举与 legacy 归一化单测。"""

import json
from pathlib import Path

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.stop_reasons import (
    CANONICAL_STOP_REASONS,
    StopReason,
    is_canonical_stop_reason,
    normalize_stop_reason,
    stop_reason_detail_from_legacy,
)
from agent_runtime.task_state import TaskState
from agent_runtime.workspace import WorkspaceContext


class TestStopReasonNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("final", "final"),
            ("step_limit", "step_limit"),
            ("tool_steps > 6", "step_limit"),
            ("tool_steps >= 3", "step_limit"),
            ("attempts >= 22", "parse_fail"),
            ("error: connection reset", "api_error"),
            ("step_timeout", "step_timeout"),
            ("custom_unknown", "custom_unknown"),
        ],
    )
    def test_normalize_legacy(self, raw, expected):
        assert normalize_stop_reason(raw) == expected

    def test_detail_extracted_for_legacy(self):
        assert stop_reason_detail_from_legacy("tool_steps > 6") == "tool_steps > 6"
        assert stop_reason_detail_from_legacy("final") == ""

    def test_canonical_members(self):
        assert is_canonical_stop_reason("final")
        assert is_canonical_stop_reason("stall")
        assert not is_canonical_stop_reason("tool_steps > 1")
        assert len(CANONICAL_STOP_REASONS) == len(StopReason)


class TestTaskStateStopReason:
    def test_stop_step_limit_canonical(self):
        ts = TaskState.create(user_request="x")
        ts.stop_step_limit(6)
        assert ts.stop_reason == StopReason.STEP_LIMIT.value
        assert ts.node_timings["stop_reason_detail"] == "tool_steps > 6"

    def test_stop_retry_limit_canonical(self):
        ts = TaskState.create(user_request="x")
        ts.stop_retry_limit(22)
        assert ts.stop_reason == StopReason.PARSE_FAIL.value
        assert "attempts" in ts.node_timings["stop_reason_detail"]

    def test_from_dict_normalizes_legacy(self):
        ts = TaskState.from_dict(
            {
                "run_id": "run-1",
                "task_id": "run-1",
                "user_request": "hi",
                "stop_reason": "tool_steps > 6",
            }
        )
        assert ts.stop_reason == StopReason.STEP_LIMIT.value
        assert ts.node_timings["stop_reason_detail"] == "tool_steps > 6"


def _latest_report(workspace: Path) -> dict:
    runs = sorted((workspace / ".agent" / "runs").iterdir(), key=lambda p: p.stat().st_mtime)
    return json.loads((runs[-1] / "report.json").read_text(encoding="utf-8"))


class TestAgentLoopStopReason:
    def test_max_steps_emits_step_limit(self, workspace, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=1, step_timeout_s=0, tool_timeout_s=0)
        client = FakeModelClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>done</final>",
            ]
        )
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            cwd=str(temp_workspace),
        )
        agent.ask("go")

        report = _latest_report(temp_workspace)
        assert report["stop_reason"] == StopReason.STEP_LIMIT.value
        assert "tool_steps" in report["node_timings"]["stop_reason_detail"]

    def test_parse_fail_emits_canonical_reason(self, workspace, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=2, step_timeout_s=0, tool_timeout_s=0)
        client = FakeModelClient(["not xml"] * 20)
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            cwd=str(temp_workspace),
        )
        agent.ask("go")

        report = _latest_report(temp_workspace)
        assert report["stop_reason"] == StopReason.PARSE_FAIL.value
