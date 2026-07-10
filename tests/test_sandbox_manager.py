"""SandboxManager + PatchApplier + TestRunner 单测（Mock docker）。"""

import json
from unittest.mock import MagicMock

from src.harness.patch_applier import PatchApplier
from src.harness.python_runner import PythonTestRunner
from src.harness.sandbox_manager import (
    ExecResult,
    SandboxManager,
    sandbox_container_run_kwargs,
    sandbox_pip_install_command,
    sandbox_tmpfs_mounts,
)
from src.state import CandidatePatch


class FakeSandbox:
    """模拟 Sandbox（不依赖 Docker）。"""

    id = "fake-id"
    profile = "python"


class FakeManager:
    """模拟 SandboxManager。"""

    def __init__(self, responses: dict[str, ExecResult] = None):
        self.responses = responses or {}
        self.calls = []

    def execute(self, sandbox, command: str, timeout: int = 600) -> ExecResult:
        self.calls.append(command)
        for key, result in self.responses.items():
            if key in command:
                return result
        return ExecResult(exit_code=0, stdout="ok", stderr="")


class TestSandboxManager:
    def test_create_returns_sandbox(self):
        mgr = SandboxManager()
        # _docker 未初始化时不应报错
        assert mgr.IMAGE == "repair-agent/python-repair"

    def test_tmpfs_mounts_include_code_and_tmp(self):
        mounts = sandbox_tmpfs_mounts()
        assert "/tmp" in mounts
        assert "/code" in mounts
        assert mounts["/tmp"].startswith("size=")
        assert mounts["/code"].startswith("size=")

    def test_container_run_kwargs_read_only_dual_tmpfs(self):
        kwargs = sandbox_container_run_kwargs("repair-agent/python-repair")
        assert kwargs["read_only"] is True
        assert set(kwargs["tmpfs"]) == {"/tmp", "/code"}
        assert kwargs["network_mode"] == "none"

    def test_pip_install_command_uses_user_site(self):
        cmd = sandbox_pip_install_command()
        assert "pip install --user -e /code" in cmd

    def test_create_passes_fs_isolation_to_docker(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1\n", encoding="utf-8")

        fake_container = MagicMock()
        fake_container.id = "container-abc"
        captured: dict = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return fake_container

        mgr = SandboxManager()
        mgr._docker = MagicMock()
        mgr._docker.containers.run = fake_run

        sandbox = mgr.create(str(repo))

        assert sandbox.id == "container-abc"
        assert captured["read_only"] is True
        assert "/code" in captured["tmpfs"]
        assert "/tmp" in captured["tmpfs"]
        fake_container.put_archive.assert_called_once()


class TestPatchApplier:
    def test_apply_single_patch(self):
        mgr = FakeManager({"patch": ExecResult(0, "patched", "")})
        applier = PatchApplier(mgr)
        patch = CandidatePatch(file_path="calc.py", diff="-old\n+new")
        results = applier.apply(FakeSandbox(), [patch])
        assert results == [True]

    def test_apply_rollback_on_failure(self):
        mgr = FakeManager({"patch_0": ExecResult(1, "", "fail")})
        applier = PatchApplier(mgr)
        patch = CandidatePatch(file_path="calc.py", diff="-old\n+new")
        results = applier.apply(FakeSandbox(), [patch])
        assert results == [False]


class TestPythonRunner:
    def test_parse_json_report(self):
        mgr = FakeManager(
            {
                "pip install": ExecResult(0, "installed", ""),
                "pytest": ExecResult(0, "tests ran", ""),
                "cat /code/.report.json": ExecResult(
                    0,
                    json.dumps(
                        {
                            "summary": {"total": 3, "passed": 2, "failed": 1, "error": 0},
                            "tests": [
                                {"nodeid": "test_add", "outcome": "passed"},
                                {
                                    "nodeid": "test_sub",
                                    "outcome": "failed",
                                    "call": {"longrepr": "AssertionError: 3!=5"},
                                },
                            ],
                        }
                    ),
                    "",
                ),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert result.total_tests == 3
        assert result.passed == 2
        assert result.failed == 1
        assert not result.all_passed

    def test_pytest_timeout_not_passed(self):
        """exec 超时时不应误判为通过。"""
        mgr = FakeManager(
            {
                "pytest": ExecResult(-1, "", "timeout after 900s"),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert not result.all_passed
        assert result.total_tests == 0
        assert result.failure_logs

    def test_pytest_exit_zero_without_report_not_passed(self):
        """pytest 0 退出但无 JSON 报告时 all_passed=False。"""
        mgr = FakeManager(
            {
                "pytest": ExecResult(0, "no tests ran", ""),
                "cat /code/.report.json": ExecResult(1, "", "No such file"),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert not result.all_passed
        assert result.total_tests == 0
