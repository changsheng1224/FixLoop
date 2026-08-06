"""sandbox_tools 单测（Mock SandboxManager）。"""

import json
from unittest.mock import MagicMock

import pytest

from agent_runtime.tool_context import ToolContext
from src.harness.sandbox_manager import ExecResult, Sandbox
from src.state import VerificationResult
from src.tools.sandbox_tools import (
    run_sandbox_verification,
    sandbox_build,
    sandbox_test,
    sandbox_verify,
)


def _patch_sandbox_available(monkeypatch):
    """Mock assert_sandbox_available 为 no-op（sandbox 测试不需要真实 Docker）。"""
    monkeypatch.setattr(
        "src.harness.sandbox_verify.assert_sandbox_available",
        lambda: None,
    )


class TestSandboxToolsValidation:
    def test_sandbox_test_missing_repo(self):
        ctx = ToolContext(root=".")
        out = sandbox_test(ctx, {})
        assert "Error" in out

    def test_sandbox_verify_missing_repo(self):
        ctx = ToolContext(root=".")
        out = sandbox_verify(ctx, {})
        assert "Error" in out


class TestSandboxToolsMocked:
    def test_sandbox_test_returns_json(self, monkeypatch):
        ctx = ToolContext(root=".")
        vr = VerificationResult(all_passed=True, total_tests=2, passed=2)
        monkeypatch.setattr(
            "src.tools.sandbox_tools._run_test_in_sandbox",
            lambda _ctx, repo, test_path: (vr, {"pytest_ms": 42}),
        )
        out = sandbox_test(ctx, {"repo_path": ".", "test_path": ""})
        data = json.loads(out)
        assert data["all_passed"] is True
        assert data["total_tests"] == 2

    def test_sandbox_verify_includes_timings(self, monkeypatch):
        ctx = ToolContext(root=".")
        vr = VerificationResult(all_passed=False, total_tests=1, failed=1)
        monkeypatch.setattr(
            "src.tools.sandbox_tools._run_test_in_sandbox",
            lambda _ctx, repo, test_path: (vr, {"pytest_ms": 10, "build_result": "ok"}),
        )
        out = sandbox_verify(ctx, {"repo_path": "."})
        data = json.loads(out)
        assert data["sandbox_timings"]["pytest_ms"] == 10

    def test_run_sandbox_verification_entry(self, monkeypatch):
        vr = VerificationResult(all_passed=True, total_tests=1, passed=1)
        monkeypatch.setattr(
            "src.tools.sandbox_tools._run_test_in_sandbox",
            lambda _ctx, repo, test_path, cancel_token=None: (vr, {}),
        )
        result, timings = run_sandbox_verification(".")
        assert result.all_passed
        assert isinstance(timings, dict)

    def test_sandbox_build_creates_container(self, monkeypatch, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        fake_mgr = MagicMock()
        fake_mgr.create.return_value = Sandbox(id="sb-1", profile="python")
        fake_mgr.execute.return_value = ExecResult(0, "installed", "")
        monkeypatch.setattr(
            "src.harness.sandbox_verify.SandboxManager",
            lambda: fake_mgr,
        )
        out = sandbox_build(ctx, {"repo_path": str(temp_workspace)})
        assert "pip install" in out or "skipped" in out
        fake_mgr.create.assert_called_once()

    def test_pip_install_uses_user_flag_when_dependencies(self, monkeypatch, temp_workspace):
        (temp_workspace / "pyproject.toml").write_text(
            '[project]\nname="t"\ndependencies=["requests"]\n',
            encoding="utf-8",
        )
        ctx = ToolContext(root=str(temp_workspace))
        fake_mgr = MagicMock()
        fake_mgr.create.return_value = Sandbox(id="sb-2", profile="python")
        fake_mgr.execute.return_value = ExecResult(0, "ok", "")
        monkeypatch.setattr(
            "src.harness.sandbox_verify.SandboxManager",
            lambda: fake_mgr,
        )
        sandbox_build(ctx, {"repo_path": str(temp_workspace)})
        cmd = fake_mgr.execute.call_args[0][1]
        assert "pip install --user -e /code" in cmd

    def test_run_sandbox_verification_tar_limit_returns_failure(self, monkeypatch, tmp_path):
        (tmp_path / "big.bin").write_bytes(b"x" * 500)
        _patch_sandbox_available(monkeypatch)
        import src.harness.sandbox_tar as sandbox_tar_mod

        original_max = sandbox_tar_mod.sandbox_tar_max_bytes
        sandbox_tar_mod.sandbox_tar_max_bytes = lambda: 100
        try:
            result, timings = run_sandbox_verification(str(tmp_path))
            assert not result.all_passed
            assert timings["tar_error_code"] == "tar_size_exceeded"
            assert "tar 打包超限" in result.failure_logs[0]
        finally:
            sandbox_tar_mod.sandbox_tar_max_bytes = original_max


class TestSandboxExecutionTier:
    """sandbox 工具 execution_tier 声明与体检。"""

    def test_sandbox_tools_have_container_tier(self):
        from src.tools.sandbox_tools import build_sandbox_tool_registry

        ctx = ToolContext(root=".")
        registry = build_sandbox_tool_registry(ctx)
        for name in ("sandbox_build", "sandbox_test", "sandbox_verify"):
            spec = registry.get(name)
            assert spec is not None, f"missing tool: {name}"
            assert spec.get("execution_tier") == "container", f"{name} should be container tier"

    def test_assert_sandbox_available_raises_when_docker_missing(self, monkeypatch):
        # 强制 import docker 时失败
        import builtins

        from src.harness.sandbox_verify import SandboxNotAvailableError, assert_sandbox_available

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "docker":
                raise ImportError("No module named 'docker'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        with pytest.raises(SandboxNotAvailableError):
            assert_sandbox_available()

    def test_sandbox_not_available_error_message(self):
        from src.harness.sandbox_verify import SandboxNotAvailableError

        exc = SandboxNotAvailableError("Docker daemon not running")
        assert "Docker sandbox 不可用" in str(exc)
        assert "Docker daemon not running" in str(exc)
        assert exc.reason == "Docker daemon not running"
