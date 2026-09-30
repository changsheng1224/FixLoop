"""P1 real Linux/bwrap checks, isolated from the ordinary test suite."""

import importlib
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

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
from agent_runtime.linux_sandbox.receipts import ReceiptStore, process_identity


@pytest.fixture
def sandbox():
    base = os.environ.get("FIXLOOP_P1_ROOT")
    if sys.platform != "linux" or not base:
        pytest.skip("requires explicitly configured native WSL P1 fixture")
    with tempfile.TemporaryDirectory(dir=base, prefix="p1-test-") as temporary:
        root = Path(temporary)
        workspace, state = root / "workspace", root / "state"
        workspace.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        helper = (
            Path(os.environ["FIXLOOP_P1_CONTROLLER_ROOT"])
            / "agent_runtime/linux_sandbox/supervisor.py"
        )
        policy = SandboxPolicy(workspace, state, Path(base) / "toolchain", helper)
        yield LinuxSandboxBackend(policy), workspace, state


def run(backend, call, code, *, timeout=5, limit=1048576):
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        call,
        "command",
        ("/toolchain/bin/python", "-I", "-c", code),
        timeout_s=timeout,
        output_limit_bytes=limit,
    )
    return backend.execute(request)


def test_mount_environment_receipt_and_replay(sandbox):
    backend, workspace, state = sandbox
    code = (
        "import os,pathlib; p=pathlib.Path; "
        "p('/workspace/result').write_text('ok'); "
        "print(os.getpid(), p('/mnt/c').exists(), "
        "p('/workspace/../state').exists(), 'WSL_INTEROP' in os.environ)"
    )
    result = run(backend, "mount", code)
    assert result.execution_status == "completed", result
    assert result.exit_code == 0
    assert result.cleanup == "confirmed"
    assert result.stdout_excerpt.strip() == "2 False False False"
    assert (workspace / "result").read_text() == "ok"
    assert (
        ReceiptStore(state, workspace).reconcile(backend.policy.digest())["result"]
        == result.to_wire()
    )
    assert run(backend, "mount", "print('again')").execution_status == "rejected"


def test_fixed_pytest_uses_the_same_backend(sandbox):
    backend, workspace, state = sandbox
    (workspace / "test_small.py").write_text("def test_ok():\n    assert 2 + 2 == 4\n")
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "pytest",
        "pytest",
        ("/toolchain/bin/python", "-I", "-m", "pytest", "-q", "test_small.py"),
        timeout_s=10,
    )
    result = backend.execute(request)
    assert result.execution_status == "completed" and result.exit_code == 0, result
    assert "1 passed" in result.stdout_excerpt
    assert (
        ReceiptStore(state, workspace).reconcile(backend.policy.digest())["result"]
        == result.to_wire()
    )


def test_output_limit_and_tmpfs_enforced(sandbox):
    backend, _, _ = sandbox
    flooded = run(
        backend, "flood", "import sys;sys.stdout.write('x'*200000);sys.stdout.flush()", limit=1024
    )
    assert flooded.execution_status == "output_limit_exceeded", flooded
    assert flooded.cleanup == "confirmed"
    full = run(
        backend, "tmp", "with open('/tmp/full','wb') as f:\n f.write(b'x'*67108865)", timeout=10
    )
    assert full.execution_status == "completed", full
    assert full.exit_code != 0 and "Errno 28" in full.stderr_excerpt
    assert full.cleanup == "confirmed"


def test_missing_toolchain_fails_closed(sandbox):
    backend, workspace, state = sandbox
    policy = SandboxPolicy(workspace, state, state / "missing", backend.policy.helper)
    bad = LinuxSandboxBackend(policy).execute(
        SandboxRequest(
            "ws",
            "task",
            "run",
            "missing",
            "command",
            ("/toolchain/bin/python", "-I", "-c", "print(1)"),
        )
    )
    assert bad.execution_status == "rejected"
    assert not ReceiptStore(state, workspace).registry.exists()


def test_missing_bwrap_fails_closed(sandbox):
    backend, workspace, state = sandbox
    policy = SandboxPolicy(
        workspace, state, backend.policy.toolchain, backend.policy.helper, bwrap=state / "missing"
    )
    bad = LinuxSandboxBackend(policy).execute(
        SandboxRequest(
            "ws",
            "task",
            "run",
            "missing-bwrap",
            "command",
            ("/toolchain/bin/python", "-I", "-c", "print(1)"),
        )
    )
    assert bad.execution_status == "rejected" and bad.error_code == "bwrap_unavailable"
    assert not ReceiptStore(state, workspace).registry.exists()


def test_credential_like_workspace_asset_is_rejected(sandbox):
    backend, workspace, state = sandbox
    (workspace / ".env.local").write_text("dummy=not-a-real-credential")
    result = run(backend, "secret", "print('not run')")
    assert result.execution_status == "rejected"
    assert result.error_code == "workspace_mapping_rejected"
    assert not ReceiptStore(state, workspace).registry.exists()


def test_toolchain_is_read_only_and_environment_is_cleared(sandbox):
    backend, _, _ = sandbox
    code = (
        "import os; print('WSL_INTEROP' in os.environ, 'HOME' in os.environ); "
        "open('/toolchain/forbidden','wb').write(b'x')"
    )
    result = run(backend, "readonly", code)
    assert result.execution_status == "completed", result
    assert result.exit_code != 0 and "Errno 30" in result.stderr_excerpt
    assert result.stdout_excerpt.strip() == "False True"


def test_receipts_are_scoped_to_workspace_and_uncertain_blocks(sandbox):
    backend, workspace, state = sandbox
    other = workspace.parent / "other"
    other.mkdir(mode=0o700)
    first, second = ReceiptStore(state, workspace), ReceiptStore(state, other)
    for store in (first, second):
        store.transition("same", "planned", policy_digest="digest")
        store.transition("same", "running")
        store.transition(
            "same", "terminal", result={"execution_status": "completed", "cleanup": "confirmed"}
        )
    assert first.receipt_path("same") != second.receipt_path("same")
    assert first.reconcile("digest") and second.reconcile("digest")
    first.transition("unknown", "planned", policy_digest="digest")
    first.transition("unknown", "running")
    with pytest.raises(ValueError, match="execution_uncertain"):
        first.reconcile("digest")


def test_receipt_id_cannot_be_reused_after_another_call(sandbox):
    _, workspace, state = sandbox
    store = ReceiptStore(state, workspace)
    for call in ("old", "new"):
        store.transition(call, "planned", policy_digest="digest")
        store.transition(call, "running")
        store.transition(
            call, "terminal", result={"execution_status": "completed", "cleanup": "confirmed"}
        )
    with pytest.raises(ValueError, match="duplicate call_id"):
        store.transition("old", "planned", policy_digest="digest")


def test_corrupt_receipt_and_reused_pid_identity_fail_closed(sandbox):
    backend, workspace, state = sandbox
    store = ReceiptStore(state, workspace)
    digest = backend.preflight()
    identity = process_identity(os.getpid())
    store.transition("stale", "planned", policy_digest=digest)
    store.transition(
        "stale",
        "running",
        supervisor_identity={**identity, "start_ticks": identity["start_ticks"] - 1},
    )
    with pytest.raises(ValueError, match="execution_uncertain"):
        backend.reconcile()
    os.kill(os.getpid(), 0)  # Reconciliation did not signal a reused PID.
    data = json.loads(store.registry.read_text())
    data["sha256"] = "0" * 64
    store.registry.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="receipt_invalid"):
        backend.reconcile()


def test_supervisor_start_failure_is_a_terminal_no_target_result(sandbox, monkeypatch):
    backend, workspace, state = sandbox
    digest = backend.preflight()
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "no-helper",
        "command",
        ("/toolchain/bin/python", "-I", "-c", "print(1)"),
    )
    store = ReceiptStore(state, workspace)
    store.transition(request.call_id, "planned", policy_digest=digest)
    backend_module = importlib.import_module("agent_runtime.linux_sandbox.backend")
    monkeypatch.setattr(
        backend_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("missing")),
    )
    result = backend._dispatch(request, digest)
    assert result.execution_status == "start_failed", result
    assert store.reconcile(digest)["no_target_started"] is True


def test_partial_supervisor_response_has_a_hard_read_deadline(sandbox):
    backend, _, _ = sandbox
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"incomplete":')
        fake = SimpleNamespace(stdout=os.fdopen(read_fd, "rb", buffering=0), poll=lambda: None)
        started = time.monotonic()
        assert backend._read_response(fake, 0.2) is None
        assert time.monotonic() - started < 0.6
    finally:
        os.close(write_fd)
        fake.stdout.close()
