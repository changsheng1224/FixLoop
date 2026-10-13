"""Native transport and commit evidence, not WSL namespace-isolation evidence."""

import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.task_context import (
    SandboxEnvironmentIdentity,
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.linux_sandbox.workspace_backend import GuardedWindowsWorkspaceBackend
from agent_runtime.linux_sandbox.workspace_worker import execute_request


@pytest.fixture
def transport(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows native transport")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_bytes(b"original")
    spec = SandboxTaskSpec(
        SandboxEnvironmentIdentity("stub", "test", "test", "a" * 64),
        "s3",
        "b" * 64,
        str(tmp_path / "state"),
    )
    binding = SandboxTaskBinding.create(root, root, "task", spec)
    backend = GuardedWindowsWorkspaceBackend(binding)
    req = SandboxRequest(
        binding.mapping.workspace_id,
        "task",
        "task",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name="read_file",
        tool_args={"path": "a.txt"},
    )
    script = tmp_path / "controller.py"
    script.write_text(
        "import json, sys\n"
        "envelope=json.loads(sys.stdin.buffer.readline())\n"
        "open(sys.argv[1]+'.ready','w').close()\n"
        "terminal=json.load(open(sys.argv[1]))\n"
        "if sys.argv[2]=='cancel':\n"
        "    sys.stdin.buffer.read()\n"
        "    terminal['cancelled']=True\n"
        "    terminal['reply']=None\n"
        "if sys.argv[2]!='eof': print(json.dumps({'terminal':terminal}), flush=True)\n",
        encoding="utf-8",
    )
    payload = tmp_path / "terminal.json"

    def setup(request=req, *, mode="normal", change=None):
        terminal = {
            "call_id": request.call_id,
            "cleanup": "confirmed",
            "cancelled": False,
            "exit_code": 0,
            "runtime_digest": "a" * 64,
            "reply": execute_request(request, root=root).to_wire(),
        }
        if change is not None:
            terminal = change(terminal)
        payload.write_text(json.dumps(terminal), encoding="utf-8")
        monkeypatch.setattr(
            backend, "_argv", lambda: [sys.executable, "-I", "-B", str(script), str(payload), mode]
        )
        backend.permissions.authorize(
            request,
            input_versions=backend.permission_inputs(request),
            restore_owned=request.base_versions,
        )
        return request

    return SimpleNamespace(
        backend=backend, root=root, request=req, setup=setup, ready=tmp_path / "terminal.json.ready"
    )


def test_read_reply_and_exact_receipt(transport):
    result = transport.backend.execute(transport.setup())
    assert result.execution_status == "completed"
    assert result.tool_result["status"] == "success"
    assert transport.backend.inspect_receipt("call")["result"] == result.to_wire()


def test_launcher_cannot_resolve_workspace_executable(transport, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    transport.backend.task_binding = replace(
        transport.backend.task_binding,
        environment=replace(
            transport.backend.task_binding.environment,
            distribution="FixLoop-Sandbox-s3-v1-aaaaaaaaaaaa",
        ),
    )
    argv = transport.backend._argv()
    assert argv[0] == "C:\\Windows\\System32\\wsl.exe"
    assert "/usr/bin/unshare" not in argv  # Measure real ownership before entering namespaces.
    assert argv[-4:] == [
        "/usr/bin/python3.14",
        "-I",
        "-B",
        "/opt/fixloop/runtime/agent_runtime/linux_sandbox/resource_supervisor.py",
    ]
    assert argv[argv.index("--user") + 1] == "root"


@pytest.mark.parametrize(
    "field,value",
    [
        ("call_id", "forged"),
        ("cleanup", "unverified"),
        ("runtime_digest", "c" * 64),
        ("cancelled", 1),
        ("exit_code", False),
    ],
)
def test_forged_terminal_is_uncertain_and_blocks_next_call(transport, field, value):
    req = transport.setup(change=lambda t: {**t, field: value})
    result = transport.backend.execute(req)
    assert result.execution_status == "uncertain" and result.cleanup != "confirmed"
    assert transport.backend.inspect_receipt("call")["result"] == result.to_wire()
    again = transport.backend.execute(replace(req, call_id="again"))
    assert again.error_code == "execution_uncertain"


@pytest.mark.parametrize(
    "mode,change",
    [("eof", None), ("normal", lambda t: []), ("normal", lambda t: {**t, "reply": {}})],
)
def test_invalid_transport_or_worker_never_succeeds(transport, mode, change):
    result = transport.backend.execute(transport.setup(mode=mode, change=change))
    assert result.execution_status != "completed" or result.tool_result["status"] == "error"
    assert result.mutation_status != "committed"


def test_cleanup_failure_preserves_actual_edit(transport, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_backend as module

    req = replace(
        transport.request,
        tool_name="write_file",
        tool_args={"path": "a.txt", "content": "new"},
        base_versions={"a.txt": hashlib.sha256(b"original").hexdigest()},
    )
    transport.setup(req)
    native_popen = module.subprocess.Popen

    class BrokenClose:
        def __init__(self, pipe):
            self.pipe = pipe

        def __getattr__(self, name):
            return getattr(self.pipe, name)

        def close(self):
            self.pipe.close()
            raise OSError("simulated teardown failure")

    def popen(*args, **kwargs):
        proc = native_popen(*args, **kwargs)
        proc.stdin = BrokenClose(proc.stdin)
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    result = transport.backend.execute(req)
    assert (transport.root / "a.txt").read_bytes() == b"new"
    assert result.execution_status == "uncertain"
    assert result.tool_result["changed_files"] == ["a.txt"]
    assert result.mutation_status == "committed"
    assert transport.backend.inspect_receipt("call")["result"] == result.to_wire()


def test_cancel_waiting_controller_preserves_terminal_cleanup(transport):
    req = transport.setup(mode="cancel")
    results = []
    thread = threading.Thread(target=lambda: results.append(transport.backend.execute(req)))
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not transport.ready.exists():
        time.sleep(0.01)
    assert transport.ready.exists()
    assert transport.backend.cancel(req.call_id)
    thread.join(timeout=12)
    assert not thread.is_alive()
    assert results[0].execution_status == "cancelled"
    assert results[0].cleanup == "confirmed"
    assert results[0].mutation_status == "not_started"


def test_cancel_blocked_input_write_is_bounded(transport, monkeypatch, tmp_path):
    import agent_runtime.linux_sandbox.workspace_backend as module

    script = tmp_path / "unresponsive.py"
    script.write_text("import time; time.sleep(30)", encoding="utf-8")
    native_popen = module.subprocess.Popen
    processes = []

    def popen(*args, **kwargs):
        proc = native_popen(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(transport.backend, "_argv", lambda: [sys.executable, "-I", str(script)])
    req = replace(
        transport.request,
        tool_name="write_file",
        tool_args={"path": "new.txt", "content": "x" * 500_000},
        base_versions={"new.txt": None},
    )
    transport.backend.permissions.authorize(
        req, input_versions=transport.backend.permission_inputs(req)
    )
    results = []
    thread = threading.Thread(
        target=lambda: results.append(transport.backend.execute(req)), daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not transport.backend.cancel(req.call_id):
            time.sleep(0.01)
        thread.join(timeout=12)
        assert not thread.is_alive(), "cancellation blocked on the stdin writer"
        assert results[0].execution_status == "uncertain"
        assert results[0].cleanup != "confirmed"
        assert not (transport.root / "new.txt").exists()
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
        thread.join(timeout=5)


def test_direct_backend_requires_authorization_before_spawn(transport, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_backend as module

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned"))
    result = transport.backend.execute(transport.request)
    assert result.execution_status == "rejected"
    assert result.error_code == "authorization_required"
    assert result.mutation_status == "not_started"


def test_terminal_journal_failure_retains_committed_files(transport, monkeypatch):
    req = replace(
        transport.request,
        tool_name="write_file",
        tool_args={"path": "a.txt", "content": "new"},
        base_versions={"a.txt": hashlib.sha256(b"original").hexdigest()},
    )
    transport.setup(req)
    original = transport.backend.store.transition

    def transition(call_id, state, **fields):
        if state == "terminal":
            raise OSError("journal unavailable")
        return original(call_id, state, **fields)

    monkeypatch.setattr(transport.backend.store, "transition", transition)
    result = transport.backend.execute(req)
    assert result.execution_status == "uncertain"
    assert result.tool_result["changed_files"] == ["a.txt"]
    assert (transport.root / "a.txt").read_bytes() == b"new"
    assert transport.backend.store.current()["state"] == "running"
