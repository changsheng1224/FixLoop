"""双层拒绝语义：metadata 归一 + report 聚合。"""

import json
from pathlib import Path

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.task_state import TaskState
from agent_runtime.tool_executor import QuotaEnforcer, ToolExecutor, build_executor_rejection_metadata
from src.agents.factory import create_localizer, create_patcher
from src.middleware import ToolGateway


def _latest_report(repo_root: str) -> dict:
    runs_dir = Path(repo_root) / ".agent" / "runs"
    run_dirs = sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    return json.loads((run_dirs[-1] / "report.json").read_text(encoding="utf-8"))


class TestExecutorRejectionMetadata:
    def test_gate1_has_executor_layer(self, workspace):
        config = AgentConfig(provider="fake", max_steps=4, approval="auto")
        agent = Agent(
            config=config,
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=workspace,
        )
        agent._tool_names = ("list_files",)
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("write_file", {"path": "x.txt", "content": "y"})
        assert result.metadata["rejection_layer"] == "executor"
        assert result.metadata["gate_id"] == 1
        assert result.metadata["tool_error_code"] == "allowed_tools"

    def test_gate4_quota_has_executor_layer(self, workspace):
        config = AgentConfig(provider="fake", max_steps=4, approval="auto")
        agent = Agent(
            config=config,
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=workspace,
        )
        quota = QuotaEnforcer(max_writes=0, max_total=50)
        executor = ToolExecutor(agent=agent, approval_policy="auto", quota=quota)
        result = executor.execute("write_file", {"path": "q.txt", "content": "x"})
        assert result.metadata["gate_id"] == 4
        assert result.metadata["tool_error_code"] == "quota_exceeded"

    def test_build_executor_rejection_metadata(self):
        meta = build_executor_rejection_metadata(7, "approval_denied", approval_policy="never")
        assert meta["rejection_layer"] == "executor"
        assert meta["gate_id"] == 7


class TestTaskStateRejectionAggregation:
    def test_gateway_permission_denied(self):
        ts = TaskState.create(user_request="test")
        ts.record_tool_rejection(
            "write_file",
            {
                "tool_status": "rejected",
                "tool_error_code": "permission_denied",
                "rejection_layer": "gateway",
            },
        )
        fields = ts.rejection_report_fields()
        assert fields["tool_rejections_by_layer"]["gateway"] == 1
        assert fields["tool_rejections_by_gate"]["gateway"] == 1
        assert fields["permission_denied_by_tool"]["write_file"] == 1

    def test_executor_gate7(self):
        ts = TaskState.create(user_request="test")
        ts.record_tool_rejection(
            "write_file",
            {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "rejection_layer": "executor",
                "gate_id": 7,
            },
        )
        fields = ts.rejection_report_fields()
        assert fields["tool_rejections_by_layer"]["executor"] == 1
        assert fields["tool_rejections_by_gate"]["7"] == 1
        assert "permission_denied_by_tool" not in fields or not fields["permission_denied_by_tool"]

    def test_success_skipped(self):
        ts = TaskState.create(user_request="test")
        ts.record_tool_rejection("list_files", {"tool_status": "success"})
        assert ts.rejection_report_fields() == {}


class TestGatewayRejectionSemantics:
    def test_permission_denied_includes_reason(self):
        gw = ToolGateway({"write_file": {"patcher"}})
        result = gw.dispatch("localizer", "write_file", lambda: "ok")
        assert result.metadata["rejection_reason"] == "role_not_allowed"


class TestReportIntegration:
    def test_ask_report_counts_gateway_denials(self, workspace):
        outputs = [
            '<tool>{"name":"write_file","args":{"path":"x.py","content":"y"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"x.py","content":"z"}}</tool>',
            "<final>done</final>",
        ]
        agent = create_localizer(FakeModelClient(outputs), workspace)
        agent.ask("try write twice")
        report = _latest_report(workspace.repo_root)

        assert report["tool_rejections_by_layer"]["gateway"] == 2
        assert report["permission_denied_by_tool"]["write_file"] == 2

    def test_ask_report_counts_executor_approval_denial(self, workspace):
        outputs = [
            '<tool>{"name":"write_file","args":{"path":"gate.txt","content":"x"}}</tool>',
            "<final>done</final>",
        ]
        agent = create_patcher(FakeModelClient(outputs), workspace, approval="never")
        agent.ask("write once")
        report = _latest_report(workspace.repo_root)

        assert report["tool_rejections_by_layer"]["executor"] == 1
        assert report["tool_rejections_by_gate"]["7"] == 1

    def test_read_only_run_omits_rejection_fields(self, workspace):
        outputs = [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>ok</final>",
        ]
        config = AgentConfig(provider="fake", max_steps=4, approval="auto")
        agent = Agent(
            config=config,
            model_client=FakeModelClient(outputs),
            workspace=workspace,
        )
        agent.ask("list only")
        report = _latest_report(workspace.repo_root)

        assert "tool_rejections_by_layer" not in report
        assert "permission_denied_by_tool" not in report
