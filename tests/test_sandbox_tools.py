"""sandbox_tools 单测（Mock SandboxManager）。"""

import json
from unittest.mock import MagicMock

from agent_runtime.tool_context import ToolContext
from src.harness.sandbox_manager import ExecResult, Sandbox
from src.state import VerificationResult
from src.tools.sandbox_tools import (
    run_sandbox_verification,
    sandbox_build,
    sandbox_test,
    sandbox_verify,
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

    def test_pip_timeout_skips_pytest(self, monkeypatch, temp_workspace):
        (temp_workspace / "pyproject.toml").write_text(
            '[project]\nname="t"\ndependencies=["requests"]\n',
            encoding="utf-8",
        )
        fake_mgr = MagicMock()
        fake_mgr.create.return_value = Sandbox(id="sb-timeout", profile="python")
        fake_mgr.execute.return_value = ExecResult(-1, "", "timeout after 600s")
        monkeypatch.setattr(
            "src.harness.sandbox_verify.SandboxManager",
            lambda: fake_mgr,
        )
        result, timings = run_sandbox_verification(str(temp_workspace))
        assert not result.all_passed
        assert "sandbox pip install timeout after 600s" in result.failure_logs[0]
        assert timings["pytest_ms"] == 0
        assert fake_mgr.execute.call_count == 1

    def test_pip_failure_skips_pytest(self, monkeypatch, temp_workspace):
        (temp_workspace / "pyproject.toml").write_text(
            '[project]\nname="t"\ndependencies=["requests"]\n',
            encoding="utf-8",
        )
        fake_mgr = MagicMock()
        fake_mgr.create.return_value = Sandbox(id="sb-fail", profile="python")
        fake_mgr.execute.return_value = ExecResult(1, "pip error output", "")
        monkeypatch.setattr(
            "src.harness.sandbox_verify.SandboxManager",
            lambda: fake_mgr,
        )
        result, timings = run_sandbox_verification(str(temp_workspace))
        assert not result.all_passed
        assert "sandbox pip install failed: exit_code=1" in result.failure_logs[0]
        assert timings["pytest_ms"] == 0
        assert fake_mgr.execute.call_count == 1
