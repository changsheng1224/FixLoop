"""Owner fencing and cancel cleanup against the native WSL sandbox fixture."""

import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox import LinuxSandboxBackend, SandboxPolicy, SandboxRequest
from agent_runtime.linux_sandbox.routing import execute_sandbox
from agent_runtime.run_coordination import RunCoordinator, StaleGenerationError
from agent_runtime.run_coordination.adapters import SandboxCallAdapter
from tests.test_linux_sandbox_lifecycle import (
    assert_stopped,
    heartbeat_code,
    wait_heartbeat,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="native WSL fixture required")


@pytest.fixture
def sandbox():
    base = os.environ.get("FIXLOOP_P1_ROOT")
    if not base:
        pytest.skip("explicit native WSL fixture required")
    with tempfile.TemporaryDirectory(dir=base, prefix="coordination-") as temporary:
        root = Path(temporary)
        workspace, state = root / "workspace", root / "state"
        workspace.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        helper = (
            Path(os.environ["FIXLOOP_P1_CONTROLLER_ROOT"])
            / "agent_runtime/linux_sandbox/supervisor.py"
        )
        yield (
            LinuxSandboxBackend(SandboxPolicy(workspace, state, Path(base) / "toolchain", helper)),
            workspace,
            state,
        )


def coordinated(backend, workspace, state):
    coordinator = RunCoordinator(
        str(workspace),
        "task",
        "run",
        state_root=str(state),
        adapters={"sandbox_call": SandboxCallAdapter(backend)},
    )
    coordinator.acquire()
    coordinator.reconcile()
    context = SimpleNamespace(
        sandbox_backend=backend,
        sandbox_uncertain=False,
        sandbox_workspace_id="ws",
        sandbox_task_id="task",
        sandbox_run_id="run",
        run_coordinator=coordinator,
        sandbox_parent_resource_id="",
        cancel_token=None,
    )
    return coordinator, context


def test_owner_envelope_and_historical_receipts_are_fenced(sandbox):
    backend, workspace, state = sandbox
    coordinator, context = coordinated(backend, workspace, state)
    first = execute_sandbox(
        context, "command", ("/toolchain/bin/python", "-I", "-c", "print('first')"), 10
    )
    assert first.cleanup == "confirmed" and first.exit_code == 0, first
    old_lease = coordinator.lease
    coordinator.finish("released")
    coordinator.acquire()
    assert coordinator.reconcile()["status"] == "active"
    stale = SandboxRequest(
        "ws",
        "task",
        "run",
        "stale-call",
        "command",
        ("/toolchain/bin/python", "-I", "-c", "open('forbidden','w').write('bad')"),
        owner_token=old_lease.owner_token,
        generation=old_lease.generation,
        coordination_revision=old_lease.coordination_revision,
    )
    result = backend.execute(stale)
    assert result.execution_status == "rejected" and result.error_code == "stale_generation", result
    assert not (workspace / "forbidden").exists()
    second = execute_sandbox(
        context, "command", ("/toolchain/bin/python", "-I", "-c", "print('second')"), 10
    )
    assert second.cleanup == "confirmed", second
    resource = next(
        r for r in coordinator.store.resources("run") if r.resource_id == first.receipt_id
    )
    assert SandboxCallAdapter(backend).reconcile(resource).confirmed


def test_cancel_between_controller_and_supervisor_never_starts_target(sandbox, monkeypatch):
    backend, workspace, state = sandbox
    coordinator, context = coordinated(backend, workspace, state)
    dispatch = backend._dispatch

    def cancel_then_dispatch(request, digest):
        coordinator.request_cancel("at_supervisor_boundary")
        return dispatch(request, digest)

    monkeypatch.setattr(backend, "_dispatch", cancel_then_dispatch)
    result = execute_sandbox(
        context,
        "command",
        ("/toolchain/bin/python", "-I", "-c", "open('forbidden', 'w').write('bad')"),
        10,
    )
    assert result.execution_status == "rejected", result
    assert result.cleanup == "confirmed" and result.mutation_status == "not_started"
    assert not (workspace / "forbidden").exists()
    receipt = backend.inspect_receipt(result.receipt_id)
    assert receipt["no_target_started"] is True
    assert coordinator.cancel().confirmed


@pytest.mark.parametrize("operation", ["command", "pytest"])
def test_cancel_waits_for_detached_command_and_test_children(sandbox, operation):
    backend, workspace, state = sandbox
    coordinator, context = coordinated(backend, workspace, state)
    if operation == "pytest":
        (workspace / "test_child.py").write_text(
            f"def test_child():\n    exec({heartbeat_code()!r})\n"
        )
        argv = ("/toolchain/bin/python", "-I", "-m", "pytest", "test_child.py", "-q")
    else:
        argv = ("/toolchain/bin/python", "-I", "-c", heartbeat_code())
    results = []
    thread = threading.Thread(
        target=lambda: results.append(execute_sandbox(context, operation, argv, 10))
    )
    thread.start()
    try:
        wait_heartbeat(workspace)
        report = coordinator.cancel("cancel-live")
        thread.join(timeout=10)
        assert report.confirmed, report
        assert not thread.is_alive()
        assert_stopped(workspace)
        with pytest.raises(StaleGenerationError):
            coordinator.assert_can_dispatch()
    finally:
        if thread.is_alive():
            for resource in coordinator.store.resources("run"):
                backend.cancel(resource.resource_id)
            thread.join(timeout=15)


def test_supervisor_crash_retains_recovery_state(sandbox):
    backend, workspace, state = sandbox
    coordinator, context = coordinated(backend, workspace, state)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            execute_sandbox(
                context, "command", ("/toolchain/bin/python", "-I", "-c", heartbeat_code()), 10
            )
        )
    )
    thread.start()
    try:
        wait_heartbeat(workspace)
        with backend._guard:
            supervisor = next(iter(backend._active.values()))
        os.kill(supervisor.pid, signal.SIGKILL)
        thread.join(timeout=10)
        assert results[0].execution_status == "uncertain", results
        report = coordinator.cancel("crash-cancel")
        assert report.status == "recovery_required", report
        assert coordinator.store.snapshot("run").status == "recovery_required"
        assert_stopped(workspace)
        replacement = RunCoordinator(
            str(workspace), "task", "run", state_root=str(state), adapters=coordinator.adapters
        )
        replacement.acquire()
        assert replacement.reconcile()["status"] == "recovery_required"
    finally:
        if thread.is_alive():
            for resource in coordinator.store.resources("run"):
                backend.cancel(resource.resource_id)
            thread.join(timeout=15)
