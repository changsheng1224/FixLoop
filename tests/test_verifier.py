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

        (tmp_path / "test_x.py").write_text(
            "def test_pass(): assert True\n", encoding="utf-8"
        )
        run = PytestVerifyStrategy().run(str(tmp_path))
        assert run.internal.get("execution_tier") == "host"

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

    def test_docker_strategy_unavailable_marks_host_tier(self, monkeypatch, tmp_path):
        from src.repair.verify import DockerVerifyStrategy
        from src.harness.sandbox_verify import SandboxNotAvailableError

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        run = DockerVerifyStrategy().run(str(tmp_path))
        assert run.error == "sandbox_unavailable"
        assert run.internal.get("execution_tier") == "host"
        assert run.internal.get("sandbox_unavailable") is True
