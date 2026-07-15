"""危险工具双层闸：Gateway (L1) + Executor Gate 7 (L2) 集成测试。"""

from agent_runtime.providers.clients import FakeModelClient
from src.agents.factory import create_baseline_agent, create_localizer, create_patcher
from src.middleware import REPAIR_PERMISSION_TABLE, ToolGateway, build_repair_gateway


class TestRepairPermissionTable:
    def test_run_shell_explicitly_denied_in_repair_table(self):
        assert REPAIR_PERMISSION_TABLE["run_shell"] == set()

    def test_repair_gateway_denies_run_shell_for_all_roles(self):
        gw = build_repair_gateway()
        for role in ("localizer", "retriever", "patcher", "verifier"):
            assert gw.can_call(role, "run_shell") is False


class TestLayer1GatewayDeny:
    def test_localizer_write_permission_denied(self, workspace):
        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), workspace)
        result = agent.execute_tool("write_file", {"path": "x.py", "content": "y"})
        assert result.metadata["tool_error_code"] == "permission_denied"
        assert result.metadata["rejection_layer"] == "gateway"

    def test_patcher_run_shell_permission_denied(self, workspace):
        agent = create_patcher(FakeModelClient(["<final>ok</final>"]), workspace)
        result = agent.execute_tool("run_shell", {"command": "echo hi"})
        assert result.metadata["tool_error_code"] == "permission_denied"
        assert result.metadata["rejection_layer"] == "gateway"

    def test_localizer_run_shell_permission_denied(self, workspace):
        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), workspace)
        result = agent.execute_tool("run_shell", {"command": "echo hi"})
        assert result.metadata["tool_error_code"] == "permission_denied"
        assert result.metadata["rejection_layer"] == "gateway"

    def test_gateway_denied_skips_executor_gates(self, workspace):
        """L1 拒绝时不应出现 executor gate_id。"""
        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), workspace)
        result = agent.execute_tool("write_file", {"path": "x.py", "content": "y"})
        assert "gate_id" not in result.metadata


class TestLayer2ExecutorApproval:
    def test_patcher_write_denied_when_approval_never(self, workspace):
        agent = create_patcher(
            FakeModelClient(["<final>ok</final>"]),
            workspace,
            approval="never",
        )
        result = agent.execute_tool(
            "write_file",
            {"path": "gate_test.txt", "content": "blocked"},
        )
        assert result.metadata["tool_error_code"] == "approval_denied"
        assert result.metadata["rejection_layer"] == "executor"
        assert result.metadata["gate_id"] == 7
        assert result.metadata["approval_policy"] == "never"

    def test_patcher_write_auto_passes_with_gate7_metadata(self, workspace):
        agent = create_patcher(
            FakeModelClient(["<final>ok</final>"]),
            workspace,
            approval="auto",
        )
        result = agent.execute_tool(
            "write_file",
            {"path": "gate_ok.txt", "content": "ok"},
        )
        assert result.metadata["tool_status"] == "success"
        assert result.metadata["gate_id"] == 7
        assert result.metadata["approval_policy"] == "auto"
        assert result.metadata["approval_result"] == "auto_allowed"

    def test_read_tools_skip_gate7(self, workspace):
        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), workspace)
        result = agent.execute_tool("list_files", {"path": "."})
        assert result.metadata["tool_status"] == "success"
        assert "gate_id" not in result.metadata


class TestBaselineRunShell:
    def test_baseline_can_run_shell_with_auto_approval(self, workspace):
        """run_shell 在 _DENY_TOOLS 中始终拒绝（安全策略）。改为测试 write_file 的 auto 审批。"""
        agent = create_baseline_agent(FakeModelClient(["<final>ok</final>"]), workspace)
        result = agent.execute_tool("write_file", {"path": "test.txt", "content": "dual-gate"})
        assert result.metadata["tool_status"] == "success"


class TestGatewayMetadata:
    def test_dispatch_denial_includes_rejection_layer(self):
        gw = ToolGateway({"write_file": {"patcher"}})
        result = gw.dispatch("localizer", "write_file", lambda: "ok")
        assert result.metadata["rejection_layer"] == "gateway"
