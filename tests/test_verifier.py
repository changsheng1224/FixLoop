"""Verifier Agent 工厂与 ToolGateway 权限测试。"""

import pytest

from agent_runtime.providers.clients import FakeModelClient
from src.agents.verifier import create_verifier


@pytest.fixture
def client():
    return FakeModelClient(['{"all_passed": true, "total_tests": 1, "passed": 1}'])


class TestVerifierAgent:
    def test_factory_registers_sandbox_tools(self, client, workspace):
        agent = create_verifier(client, workspace)
        assert "sandbox_build" in agent.tools
        assert "sandbox_test" in agent.tools
        assert "sandbox_verify" in agent.tools

    def test_verifier_blocked_from_write(self, client, workspace):
        agent = create_verifier(client, workspace)
        result = agent.execute_tool("write_file", {"path": "x.py", "content": "y"})
        assert result.metadata["tool_error_code"] == "permission_denied"

    def test_verifier_blocked_from_ast_parse(self, client, workspace):
        agent = create_verifier(client, workspace)
        result = agent.execute_tool("ast_parse", {"path": "x.py"})
        assert result.metadata["tool_error_code"] == "permission_denied"

    def test_verifier_ask_returns_answer(self, client, workspace):
        agent = create_verifier(client, workspace)
        answer = agent.ask("验证补丁")
        assert "all_passed" in answer


class TestVerifyStrategyExecutionTier:
    """Verify strategy execution_tier 标记测试。"""

    def test_pytest_strategy_marks_host_tier(self, tmp_path):
        from src.repair.verify import PytestVerifyStrategy

        (tmp_path / "test_x.py").write_text("def test_pass(): assert True\n", encoding="utf-8")
        run = PytestVerifyStrategy().run(str(tmp_path))
        assert run.internal.get("execution_tier") == "host"
        assert run.internal.get("requested_tier") == "host"
        assert run.internal.get("actual_tier") == "host"
        assert run.internal.get("isolation_level") == "trusted_local"
        assert run.internal.get("trusted_execution") is True
        assert "no sandbox isolation" in run.internal.get("warning", "").lower()

    def test_docker_strategy_returns_container_tier_on_success(self, monkeypatch, tmp_path):
        from src.repair.verify import DockerVerifyStrategy

        # 跳过体检（patch 到 sandbox_verify 模块中）
        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: None,
        )
        # Mock 整个 sandbox 验证流程
        from src.state import VerificationResult

        monkeypatch.setattr(
            "src.tools.sandbox_tools.run_sandbox_verification",
            lambda repo_path, test_path="", context=None, cancel_token=None: (
                VerificationResult(all_passed=True, total_tests=1, passed=1),
                {"pytest_ms": 10},
            ),
        )
        run = DockerVerifyStrategy().run(str(tmp_path))
        assert run.error is None
        assert run.internal.get("execution_tier") == "container"
        assert run.internal.get("requested_tier") == "container"
        assert run.internal.get("actual_tier") == "container"
        assert run.internal.get("isolation_level") == "container"
        assert run.internal.get("trusted_execution") is False

    def test_docker_strategy_unavailable_marks_no_actual_tier(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.repair.verify import DockerVerifyStrategy

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        run = DockerVerifyStrategy().run(str(tmp_path))
        assert run.error == "sandbox_unavailable"
        assert run.internal.get("execution_tier") == "container"
        assert run.internal.get("requested_tier") == "container"
        assert run.internal.get("actual_tier") == "none"
        assert run.internal.get("fallback_candidate") == "host"
        assert run.internal.get("isolation_level") == "none"
        assert run.internal.get("sandbox_unavailable") is True

    def test_static_strategy_checks_syntax_without_pytest(self, tmp_path):
        from src.repair.verify import StaticVerifyStrategy

        (tmp_path / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        run = StaticVerifyStrategy().run(str(tmp_path))

        assert run.result.all_passed is True
        assert run.internal.get("execution_tier") == "static"
        assert run.internal.get("actual_tier") == "static"
        assert run.internal.get("isolation_level") == "non_executing"
        assert run.internal.get("trusted_execution") is False

    def test_static_strategy_reports_compile_error(self, tmp_path):
        from src.repair.verify import StaticVerifyStrategy

        (tmp_path / "broken.py").write_text("def bad(:\n", encoding="utf-8")
        run = StaticVerifyStrategy().run(str(tmp_path))

        assert run.result.all_passed is False
        assert run.error == "static_verify_failed"
        assert any("broken.py" in log for log in run.result.failure_logs)


class TestVerifierFallbackPolicy:
    def test_docker_unavailable_falls_back_to_host_when_allowed(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.orchestrator import Orchestrator
        from src.state import RepairState

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        (tmp_path / "test_x.py").write_text("def test_pass(): assert True\n", encoding="utf-8")
        orch = Orchestrator(None, None, None, verifier=object(), use_pytest_verify=True)
        orch._repo_root = str(tmp_path)

        result = orch._run_verifier(RepairState(issue_input="x"))

        assert result.all_passed is True

    def test_docker_unavailable_uses_static_when_host_not_allowed(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.orchestrator import Orchestrator
        from src.state import RepairState

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        (tmp_path / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        orch = Orchestrator(
            None,
            None,
            None,
            verifier=object(),
            use_pytest_verify=False,
            allow_static_verify_fallback=True,
        )
        orch._repo_root = str(tmp_path)
        state = RepairState(issue_input="x")

        result = orch._run_verifier(state)

        assert result.all_passed is True
        assert state.node_timings["phases_internal"]["verify"]["actual_tier"] == "static"

    def test_require_sandbox_blocks_fallback(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.orchestrator import Orchestrator
        from src.state import RepairState

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        orch = Orchestrator(
            None,
            None,
            None,
            verifier=object(),
            use_pytest_verify=True,
            require_sandbox=True,
        )
        orch._repo_root = str(tmp_path)
        state = RepairState(issue_input="x")

        result = orch._run_verifier(state)

        assert result.all_passed is False
        assert state.node_timings["phases_internal"]["verify"]["actual_tier"] == "none"
        assert state.agent_errors["verifier"] == "sandbox_unavailable"

    def test_static_strategy_runs_without_verifier_agent(self, tmp_path):
        from src.orchestrator import Orchestrator
        from src.state import RepairState

        (tmp_path / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        orch = Orchestrator(
            None,
            None,
            None,
            verifier=None,
            use_pytest_verify=False,
            allow_static_verify_fallback=True,
        )
        orch._repo_root = str(tmp_path)
        state = RepairState(issue_input="x")

        result = orch._run_verifier(state)

        assert result.all_passed is True
        assert state.node_timings["phases_internal"]["verify"]["actual_tier"] == "static"
