"""SandboxManager + PatchApplier + TestRunner 单测（Mock docker）。"""

import base64
import io
import json
import tarfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.harness.patch_applier import PatchApplier
from src.harness.python_runner import PythonTestRunner
from src.harness.sandbox_manager import (
    EXEC_TIMEOUT_EXIT_CODE,
    ExecResult,
    Sandbox,
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

    def execute(self, sandbox, command: str, timeout: int = 600, cancel_token=None) -> ExecResult:
        self.calls.append(command)
        for key, result in self.responses.items():
            if key in command:
                return result
        return ExecResult(exit_code=0, stdout="ok", stderr="")


class FakeUploadSocket:
    """收集 Docker exec stdin 上传的数据。"""

    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def sendall(self, data: bytes):
        self.data.extend(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        return b""

    def close(self):
        self.closed = True


def _successful_upload_results(sock: FakeUploadSocket):
    return [
        SimpleNamespace(output=sock),
        SimpleNamespace(exit_code=0, output=b""),
    ]


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
        # nobody(65534) 写入 /code 需要 world-writable tmpfs
        assert "mode=1777" in mounts["/tmp"]
        assert "mode=1777" in mounts["/code"]

    def test_tmpfs_env_size_only_still_gets_mode_1777(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_SANDBOX_TMPFS_TMP", "size=256m")
        monkeypatch.setenv("FIXLOOP_SANDBOX_TMPFS_CODE", "size=1g")
        mounts = sandbox_tmpfs_mounts()
        assert mounts["/tmp"] == "size=256m,mode=1777"
        assert mounts["/code"] == "size=1g,mode=1777"

    def test_container_run_kwargs_read_only_dual_tmpfs(self):
        kwargs = sandbox_container_run_kwargs("repair-agent/python-repair")
        assert kwargs["read_only"] is True
        assert set(kwargs["tmpfs"]) == {"/tmp", "/code"}
        assert "mode=1777" in kwargs["tmpfs"]["/code"]
        assert kwargs["network_mode"] == "none"

    def test_container_run_kwargs_uses_non_root_without_capabilities(self):
        kwargs = sandbox_container_run_kwargs("repair-agent/python-repair")
        assert kwargs["user"] == "65534:65534"
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        assert kwargs["environment"]["HOME"] == "/tmp/fixloop-home"
        assert kwargs["environment"]["PYTHONUSERBASE"] == "/tmp/fixloop-userbase"

    def test_pip_install_command_uses_user_site(self):
        cmd = sandbox_pip_install_command()
        assert "pip install --user -e /code" in cmd
        assert "python -m pip" in cmd
        assert "mkdir -p /tmp/fixloop-home /tmp/fixloop-userbase" in cmd
        assert "exit $ec" in cmd
        assert "| tail -20" not in cmd
        assert "PYTHONPATH" in cmd

    def test_execute_wraps_command_in_shell(self):
        from src.harness.sandbox_manager import sandbox_shell_argv

        mgr = SandboxManager()
        fake_container = MagicMock()
        fake_container.exec_run.return_value = (0, b"ok")
        mgr._docker = MagicMock()
        mgr._docker.containers.get.return_value = fake_container

        result = mgr.execute(Sandbox(id="sb-1", profile="python"), "echo hi && true", timeout=5)

        assert result.exit_code == 0
        argv = fake_container.exec_run.call_args[0][0]
        assert argv == sandbox_shell_argv("echo hi && true")
        assert argv[0:2] == ["/bin/sh", "-c"]
        assert "&&" in argv[2]

    def test_create_passes_fs_isolation_to_docker(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1\n", encoding="utf-8")

        fake_container = MagicMock()
        fake_container.id = "container-abc"
        fake_sock = FakeUploadSocket()
        fake_container.exec_run.side_effect = _successful_upload_results(fake_sock)
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
        fake_container.put_archive.assert_not_called()
        upload_call = fake_container.exec_run.call_args_list[0]
        assert upload_call.kwargs == {"stdin": True, "socket": True, "tty": True}
        assert "base64 -d | tar -C /code -xf -" in upload_call.args[0][2]
        assert fake_sock.data.endswith(b"\x04")
        assert fake_sock.closed
        assert sandbox.timings["tar_file_count"] == 1
        assert sandbox.timings["tar_bytes"] > 0

    def test_create_uploads_repo_contents_into_code_tmpfs(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1\n", encoding="utf-8")

        fake_container = MagicMock()
        fake_container.id = "container-code"
        fake_sock = FakeUploadSocket()
        fake_container.exec_run.side_effect = _successful_upload_results(fake_sock)

        mgr = SandboxManager()
        mgr._docker = MagicMock()
        mgr._docker.containers.run.return_value = fake_container

        mgr.create(str(repo))

        assert fake_sock.data.endswith(b"\x04")
        tar_bytes = base64.decodebytes(bytes(fake_sock.data[:-1]))
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            names = {m.name for m in tar.getmembers()}
        assert "main.py" in names
        assert "code/main.py" not in names

    def test_create_skips_docker_when_tar_too_large(self, tmp_path):
        (tmp_path / "huge.bin").write_bytes(b"x" * 500)
        mgr = SandboxManager()
        mgr._docker = MagicMock()

        import src.harness.sandbox_tar as sandbox_tar_mod

        original_max = sandbox_tar_mod.sandbox_tar_max_bytes
        sandbox_tar_mod.sandbox_tar_max_bytes = lambda: 100
        try:
            with pytest.raises(sandbox_tar_mod.SandboxArchiveError):
                mgr.create(str(tmp_path))
        finally:
            sandbox_tar_mod.sandbox_tar_max_bytes = original_max

        mgr._docker.containers.run.assert_not_called()

    def test_create_kills_container_when_exec_upload_fails(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1\n", encoding="utf-8")

        fake_container = MagicMock()
        fake_container.id = "container-fail"
        fake_container.exec_run.side_effect = RuntimeError("upload failed")

        mgr = SandboxManager()
        mgr._docker = MagicMock()
        mgr._docker.containers.run.return_value = fake_container

        with pytest.raises(RuntimeError, match="upload failed"):
            mgr.create(str(repo))
        fake_container.kill.assert_called_once()

    def test_execute_timeout_kills_container(self):
        mgr = SandboxManager()
        fake_container = MagicMock()

        def slow_exec(*_args, **_kwargs):
            time.sleep(2)
            return (0, b"done")

        fake_container.exec_run = slow_exec
        mgr._docker = MagicMock()
        mgr._docker.containers.get.return_value = fake_container

        result = mgr.execute(Sandbox(id="sb-1", profile="python"), "sleep 2", timeout=1)

        assert result.exit_code == EXEC_TIMEOUT_EXIT_CODE
        assert "timeout after 1s" in result.stderr
        fake_container.kill.assert_called_once()


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
        """exec 超时时不应误判为通过，且 failure_logs 含明确超时文案。"""
        mgr = FakeManager(
            {
                "pytest": ExecResult(-1, "", "timeout after 900s"),
                "cat /code/.report.json": ExecResult(1, "", "No such file"),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert not result.all_passed
        assert result.total_tests == 0
        assert "sandbox pytest timeout after 900s" in result.failure_logs[0]
        assert "timeout after 900s" in result.failure_logs[1]
        assert len(mgr.calls) == 1
        assert mgr.calls[0].startswith("/entrypoint.sh test")

    def test_pytest_timeout_includes_partial_stdout(self):
        mgr = FakeManager(
            {
                "pytest": ExecResult(-1, "partial output\n" * 20, "timeout after 900s"),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert any("partial output" in log for log in result.failure_logs)

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
        assert any("verify_config:" in log or "未生成可解析" in log for log in result.failure_logs)
