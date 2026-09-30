"""P1 cancellation and detached-process behavior in the native WSL fixture."""

import os
import signal
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

if (
    sys.platform == "linux"
    and os.environ.get("FIXLOOP_P1_CONTROLLER_ROOT")
    and "agent_runtime" not in sys.modules
):
    root = Path(os.environ["FIXLOOP_P1_CONTROLLER_ROOT"])
    package = types.ModuleType("agent_runtime")
    package.__path__ = [str(root / "agent_runtime")]
    sys.modules["agent_runtime"] = package
    sys.path.insert(0, str(root))

from agent_runtime.linux_sandbox import LinuxSandboxBackend, SandboxPolicy, SandboxRequest
from agent_runtime.linux_sandbox.receipts import ReceiptStore


@pytest.fixture
def sandbox():
    base = os.environ.get("FIXLOOP_P1_ROOT")
    if sys.platform != "linux" or not base:
        pytest.skip("requires explicitly configured native WSL P1 fixture")
    with tempfile.TemporaryDirectory(dir=base, prefix="p1-life-") as temporary:
        root = Path(temporary)
        workspace, state = root / "workspace", root / "state"
        workspace.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        helper = (
            Path(os.environ["FIXLOOP_P1_CONTROLLER_ROOT"])
            / "agent_runtime/linux_sandbox/supervisor.py"
        )
        backend = LinuxSandboxBackend(
            SandboxPolicy(workspace, state, Path(base) / "toolchain", helper)
        )
        yield backend, workspace, state


def request(call, code, timeout=5):
    return SandboxRequest(
        "ws",
        "task",
        "run",
        call,
        "command",
        ("/toolchain/bin/python", "-I", "-c", code),
        timeout_s=timeout,
    )


def heartbeat_code():
    child = (
        "import os,time,pathlib;os.setsid();p=pathlib.Path('/workspace/heartbeat');"
        "exec('while True: p.write_text(str(time.monotonic())); time.sleep(.05)')"
    )
    return (
        "import subprocess,time;"
        f"subprocess.Popen(['/toolchain/bin/python','-I','-c',{child!r}]);"
        "time.sleep(10)"
    )


def double_fork_code():
    return (
        "import os,time,pathlib\n"
        "p=pathlib.Path('/workspace/heartbeat')\n"
        "if os.fork()==0:\n"
        " os.setsid()\n"
        " if os.fork()==0:\n"
        "  while True:\n"
        "   p.write_text(str(time.monotonic()))\n"
        "   time.sleep(.05)\n"
        " os._exit(0)\n"
        "while not p.exists(): time.sleep(.01)\n"
    )


def wait_heartbeat(workspace, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (workspace / "heartbeat").exists():
            return
        time.sleep(0.03)
    pytest.fail("detached child never started")


def assert_stopped(workspace):
    before = (workspace / "heartbeat").read_text()
    time.sleep(0.3)
    assert (workspace / "heartbeat").read_text() == before


def test_timeout_kills_detached_child(sandbox):
    backend, workspace, _ = sandbox
    result = backend.execute(request("timeout", heartbeat_code(), timeout=1))
    assert result.execution_status == "timeout", result
    assert result.cleanup == "confirmed"
    assert_stopped(workspace)


def test_double_fork_and_pipe_holder_are_reaped(sandbox):
    backend, workspace, _ = sandbox
    result = backend.execute(request("double", double_fork_code(), timeout=1))
    assert result.execution_status in {"completed", "timeout"}, result
    assert result.cleanup == "confirmed", result
    assert (workspace / "heartbeat").exists()
    assert_stopped(workspace)


def test_controller_death_cleans_up_without_next_call(sandbox):
    backend, workspace, state = sandbox
    pid = os.fork()
    if pid == 0:
        backend.execute(request("controller", heartbeat_code(), timeout=10))
        os._exit(0)
    try:
        wait_heartbeat(workspace)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        deadline = time.monotonic() + 5
        receipt = ReceiptStore(state, workspace)
        while time.monotonic() < deadline:
            current = receipt.current()
            if current and current["state"] == "terminal":
                break
            time.sleep(0.05)
        assert current["state"] == "terminal", current
        assert current["result"]["execution_status"] == "cancelled", current
        assert current["result"]["cleanup"] == "confirmed", current
        assert_stopped(workspace)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_cancel_and_workspace_lock(sandbox):
    backend, workspace, _ = sandbox
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            backend.execute(request("cancel", heartbeat_code(), timeout=10))
        )
    )
    thread.start()
    try:
        wait_heartbeat(workspace)
        busy = backend.execute(request("other", "print('not run')"))
        assert busy.error_code == "workspace_busy", busy
        assert backend.cancel("cancel")
        thread.join(timeout=8)
        assert outcome[0].execution_status == "cancelled", outcome
        assert outcome[0].cleanup == "confirmed"
        assert_stopped(workspace)
    finally:
        if thread.is_alive():
            backend.cancel("cancel")
            thread.join(timeout=12)


def test_cancelling_one_workspace_preserves_another(sandbox):
    backend, workspace, state = sandbox
    other = workspace.parent / "other-workspace"
    other.mkdir(mode=0o700)
    other_backend = LinuxSandboxBackend(
        SandboxPolicy(other, state, backend.policy.toolchain, backend.policy.helper)
    )
    first, second = [], []
    first_thread = threading.Thread(
        target=lambda: first.append(backend.execute(request("first", heartbeat_code(), timeout=10)))
    )
    second_thread = threading.Thread(
        target=lambda: second.append(
            other_backend.execute(
                request(
                    "second",
                    "import time,pathlib;time.sleep(1);"
                    "pathlib.Path('/workspace/other-result').write_text('ok')",
                    timeout=5,
                )
            )
        )
    )
    first_thread.start()
    try:
        wait_heartbeat(workspace)
        second_thread.start()
        assert backend.cancel("first")
        first_thread.join(timeout=8)
        second_thread.join(timeout=8)
        assert first[0].execution_status == "cancelled", first
        assert second[0].execution_status == "completed", second
        assert second[0].exit_code == 0 and (other / "other-result").read_text() == "ok"
        assert_stopped(workspace)
    finally:
        if first_thread.is_alive():
            backend.cancel("first")
            first_thread.join(timeout=12)
        if second_thread.is_alive():
            other_backend.cancel("second")
            second_thread.join(timeout=12)


def test_supervisor_death_blocks_next_execution(sandbox):
    backend, workspace, state = sandbox
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            backend.execute(request("crash", heartbeat_code(), timeout=10))
        )
    )
    thread.start()
    try:
        wait_heartbeat(workspace)
        with backend._guard:
            supervisor = backend._active["crash"]
        os.kill(supervisor.pid, signal.SIGKILL)
        thread.join(timeout=8)
        assert outcome[0].execution_status == "uncertain", outcome
        assert ReceiptStore(state, workspace).current()["state"] == "running"
        assert backend.execute(request("next", "print('not run')")).execution_status == "rejected"
        assert_stopped(workspace)
    finally:
        if thread.is_alive():
            backend.cancel("crash")
            thread.join(timeout=12)
