"""Windows transport never places project arguments on the host command line."""

import io
import json
from dataclasses import replace

import pytest

from agent_runtime.linux_sandbox import SandboxRequest, WindowsWslBackend, WslLauncherConfig


class Input(io.BytesIO):
    def close(self):
        self.payload = self.getvalue()
        super().close()


class Process:
    def __init__(self, response):
        self.stdin = Input()
        self.stdout = io.BytesIO((json.dumps(response) + "\n").encode())
        self.stderr = None
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def test_launcher_argv_is_fixed_and_request_goes_only_to_stdin(monkeypatch):
    from agent_runtime.linux_sandbox import wsl_launcher

    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call-1",
        "command",
        ("/toolchain/bin/python", "-I", "-c", "print('user code')"),
    )
    captured = {}
    process = Process(
        {
            "execution_status": "completed",
            "exit_code": 0,
            "cleanup": "confirmed",
            "receipt_id": "call-1",
            "actual_backend": "linux_sandbox",
        }
    )

    def start(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return process

    monkeypatch.setattr(wsl_launcher.subprocess, "Popen", start)
    backend = WindowsWslBackend(
        WslLauncherConfig(
            "Ubuntu",
            "/trusted/bin/python",
            "/trusted/controller.py",
            "/trusted/config.json",
        )
    )
    result = backend.execute(request)
    assert result.exit_code == 0
    assert captured["argv"] == [
        "wsl.exe",
        "--distribution",
        "Ubuntu",
        "--exec",
        "/trusted/bin/python",
        "-I",
        "/trusted/controller.py",
        "/trusted/config.json",
    ]
    assert captured["kwargs"]["close_fds"] is True
    assert json.loads(process.stdin.payload)["argv"][-1] == "print('user code')"


def test_launcher_rejects_untrusted_paths():
    for path in ("relative.py", "/trusted/../workspace/controller.py", "/bad\x00path"):
        config = WslLauncherConfig("Ubuntu", "/python", path, "/config")
        try:
            config.argv()
        except ValueError:
            pass
        else:
            raise AssertionError(path)


@pytest.mark.parametrize(
    "field", ["owner_token", "generation", "coordination_revision", "receipt_id"]
)
def test_bound_launcher_rejects_response_from_other_execution(monkeypatch, field):
    from agent_runtime.linux_sandbox import wsl_launcher
    from agent_runtime.linux_sandbox.models import SandboxResult

    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "command",
        ("/toolchain/bin/python", "-I", "-c", "pass"),
        owner_token="owner",
        generation=2,
        coordination_revision=5,
    )
    response = SandboxResult(
        "completed",
        cleanup="confirmed",
        receipt_id="call",
        actual_backend="linux_sandbox",
        owner_token="owner",
        generation=2,
        coordination_revision=5,
    )
    response = replace(
        response, **{field: "wrong" if field in {"owner_token", "receipt_id"} else 99}
    )
    process = Process(response.to_wire())
    monkeypatch.setattr(wsl_launcher.subprocess, "Popen", lambda *a, **kw: process)
    backend = WindowsWslBackend(WslLauncherConfig("Ubuntu", "/python", "/controller", "/config"))
    assert backend.execute(request).execution_status == "uncertain"


def test_receipt_inspection_uses_exact_call_read_only_message(monkeypatch):
    from types import SimpleNamespace

    from agent_runtime.linux_sandbox import wsl_launcher

    captured = {}
    receipt = {"call_id": "historical", "state": "terminal"}

    def inspect(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"receipt": receipt}))

    monkeypatch.setattr(wsl_launcher.subprocess, "run", inspect)
    config = WslLauncherConfig("Ubuntu", "/python", "/controller", "/config")
    assert WindowsWslBackend(config).inspect_receipt("historical") == receipt
    assert captured["argv"] == config.argv()
    assert json.loads(captured["input"]) == {"control": "inspect_receipt", "call_id": "historical"}
