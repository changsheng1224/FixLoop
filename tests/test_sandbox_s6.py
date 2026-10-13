"""Durable lifecycle fences; real cgroup limits are covered by the WSL probe."""

import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.lifecycle import assert_task_live, lifecycle_path
from agent_runtime.linux_sandbox.models import SandboxRequest, SandboxResult
from agent_runtime.linux_sandbox.receipts import _write
from agent_runtime.linux_sandbox.resource_supervisor import dependency_usage, execution_key
from agent_runtime.linux_sandbox.task_context import (
    SandboxEnvironmentIdentity,
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.linux_sandbox.workspace_backend import GuardedWindowsWorkspaceBackend
from agent_runtime.run_coordination.coordinator import RunCoordinator


@pytest.fixture
def backend(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "user.txt").write_text("dirty user data")
    spec = SandboxTaskSpec(
        SandboxEnvironmentIdentity("stub", "test", "s6", "a" * 64),
        "s6",
        "b" * 64,
        str(tmp_path / "state"),
    )
    binding = SandboxTaskBinding.create(workspace, workspace, "task", spec)
    binding.save()
    instance = GuardedWindowsWorkspaceBackend(binding)
    monkeypatch.setattr(
        instance,
        "_maintenance",
        lambda payload: {"cleanup": "confirmed", "key": payload.get("key", "native-task")},
    )
    return instance


def abandoned(backend, *, mutations=False, state="running"):
    binding = backend.task_binding
    backend.store.transition(
        "old-call",
        "planned",
        policy_digest=binding.policy_digest,
        mapping_id=binding.mapping.mapping_id,
        environment_id=binding.environment.identity_id,
        workspace_id=binding.mapping.workspace_id,
        task_id=binding.task_id,
        run_id=binding.run_id,
        owner_token="previous-owner",
        generation=1,
    )
    if state == "running":
        backend.store.transition("old-call", "running")
    if mutations:
        backend.store.record_mutation("old-call", {"path": "user.txt", "phase": "intent"})


@pytest.mark.parametrize("state", ["planned", "running"])
def test_restart_reconciles_exact_call_without_replaying_it(backend, state):
    abandoned(backend, state=state)
    current = backend.recover()
    assert current["state"] == "terminal"
    assert current["result"]["cleanup"] == "confirmed"
    assert current["result"]["execution_status"] == "cancelled"
    assert current["owner_token"] == "previous-owner"
    assert backend.recover() == current


def test_restart_with_commit_intent_remains_blocked_and_preserves_evidence(backend):
    abandoned(backend, mutations=True)
    with pytest.raises(ValueError, match="execution_uncertain"):
        backend.recover()
    current = backend.store.inspect("old-call")
    assert current["mutation_events"] == [{"path": "user.txt", "phase": "intent"}]
    assert current["result"]["cleanup"] == "confirmed"
    assert current["result"]["mutation_status"] == "unknown"
    with pytest.raises(ValueError, match="execution_uncertain"):
        backend.retire()


@pytest.mark.parametrize("mutations", [False, True])
def test_terminal_transport_failure_can_confirm_cleanup_without_erasing_evidence(
    backend, mutations
):
    abandoned(backend, mutations=mutations)
    original = SandboxResult("uncertain", cleanup="unverified", mutation_status="unknown").to_wire()
    backend.store.transition("old-call", "terminal", result=original)
    if mutations:
        with pytest.raises(ValueError, match="execution_uncertain"):
            backend.recover()
    else:
        backend.recover()
    current = backend.store.inspect("old-call")
    assert current["interrupted_result"] == original
    assert current["result"]["cleanup"] == "confirmed"
    assert current["result"]["execution_status"] == ("uncertain" if mutations else "cancelled")


def test_recovery_identity_mismatch_cannot_touch_native_execution(backend, monkeypatch):
    abandoned(backend)
    current = backend.store.current()
    _write(backend.store.registry, {**current, "mapping_id": "changed"})
    monkeypatch.setattr(backend, "_maintenance", lambda _: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="resume_policy_mismatch"):
        backend.recover()


def test_unconfirmed_native_cleanup_cannot_terminalize_receipt(backend, monkeypatch):
    abandoned(backend)
    monkeypatch.setattr(backend, "_maintenance", lambda _: {"cleanup": "confirmed", "key": "other"})
    with pytest.raises(ValueError, match="execution_uncertain"):
        backend.recover()
    assert backend.store.current()["state"] == "running"


def test_cross_owner_coordinator_recovers_old_resource(backend):
    from agent_runtime.run_coordination.adapters import SandboxCallAdapter
    from agent_runtime.run_coordination.models import ResourceRecord

    abandoned(backend)
    resource = ResourceRecord(
        "old-call",
        "task",
        "task",
        "ws",
        kind="sandbox_call",
        owner_token="previous-owner",
        generation=1,
        status="running",
    )
    # The adapter checks the original owner identity before adopting the result.
    resource = replace(resource, payload={"owner_token": "previous-owner", "generation": 1})
    result = SandboxCallAdapter(backend).reconcile(resource)
    assert result.status == "cancelled" and result.cleanup == "confirmed"


def test_owned_versions_survive_new_backend_and_detect_missing_pair(backend):
    for index, version in enumerate(["1" * 64, "2" * 64, None]):
        call = "commit-" + str(index)
        backend.store.transition(call, "planned", policy_digest=backend.task_binding.policy_digest)
        backend.store.transition(call, "running")
        backend.store.transition(
            call,
            "terminal",
            result=SandboxResult(
                "completed",
                cleanup="confirmed",
                mutation_status="committed",
                tool_result={"metadata": {"committed_versions": {"user.txt": version}}},
            ).to_wire(),
        )
    restarted = GuardedWindowsWorkspaceBackend(backend.task_binding)
    assert restarted.owned_versions() == {"user.txt": None}
    restarted.store.receipt_path("commit-2").unlink()
    with pytest.raises(ValueError, match="receipt_invalid"):
        restarted.owned_versions()


def test_retirement_preserves_workspace_state_and_is_idempotent(backend):
    binding = backend.task_binding
    state = backend.retire()
    assert state["state"] == "retired"
    assert backend.retire() == state
    assert (Path(binding.mapping.host_root) / "user.txt").read_text() == "dirty user data"
    assert (lifecycle_path(binding).parent / "sandbox-task.json").exists()
    with pytest.raises(ValueError, match="sandbox_task_retired"):
        assert_task_live(binding)
    with pytest.raises(ValueError, match="sandbox_task_retired"):
        GuardedWindowsWorkspaceBackend(binding)


def test_active_owner_blocks_retirement_until_released(backend):
    binding = backend.task_binding
    coordinator = RunCoordinator(
        binding.mapping.host_root, binding.task_id, binding.run_id, state_root=binding.state_root
    )
    coordinator.acquire()
    coordinator.reconcile()
    with pytest.raises(ValueError, match="run not terminal"):
        backend.retire()
    assert not lifecycle_path(binding).exists()
    coordinator.finish("released")
    assert backend.retire()["state"] == "retired"


def test_pending_approval_blocks_collection(backend):
    from agent_runtime.linux_sandbox.permissions import PermissionScope

    backend.permissions.replace_scope(PermissionScope(capabilities=("workspace_read",)))
    binding = backend.task_binding
    request = SandboxRequest(
        binding.mapping.workspace_id,
        "task",
        "task",
        "pending",
        "command",
        ("/toolchain/bin/python", "-c", "print(1)"),
        output_limit_bytes=65536,
    )
    assert backend.permissions.authorize(request)["status"] == "approval_required"
    with pytest.raises(ValueError, match="permission pending"):
        backend.retire()
    backend.permissions.cancel_pending()
    assert backend.retire()["state"] == "retired"


def test_failed_collection_closes_admission_and_can_retry(backend, monkeypatch):
    def fail(_):
        raise ValueError("execution_uncertain")

    monkeypatch.setattr(backend, "_maintenance", fail)
    with pytest.raises(ValueError, match="execution_uncertain"):
        backend.retire()
    with pytest.raises(ValueError, match="sandbox_task_retired"):
        assert_task_live(backend.task_binding)
    monkeypatch.setattr(backend, "_maintenance", lambda _: {"cleanup": "confirmed"})
    assert backend.retire()["state"] == "retired"


def test_interrupted_retirement_resumes_in_new_process_object(backend, monkeypatch):
    monkeypatch.setattr(backend, "_maintenance", lambda _: {"cleanup": "unverified"})
    with pytest.raises(ValueError, match="native cleanup unconfirmed"):
        backend.retire()
    monkeypatch.setattr(
        GuardedWindowsWorkspaceBackend, "_maintenance", lambda *_: {"cleanup": "confirmed"}
    )
    assert (
        GuardedWindowsWorkspaceBackend.resume_retirement(backend.task_binding)["state"] == "retired"
    )


def test_retirement_fences_new_approvals_and_new_owners(backend):
    from agent_runtime.run_coordination.store import CoordinationError

    backend.retire()
    binding = backend.task_binding
    request = SandboxRequest(
        binding.mapping.workspace_id,
        "task",
        "task",
        "late",
        "command",
        ("/toolchain/bin/python", "-c", "print(1)"),
        output_limit_bytes=65536,
    )
    with pytest.raises(ValueError, match="sandbox_task_retired"):
        backend.permissions.authorize(request)
    coordinator = RunCoordinator(
        binding.mapping.host_root, binding.task_id, binding.run_id, state_root=binding.state_root
    )
    with pytest.raises(CoordinationError, match="sandbox_task_retired"):
        coordinator.acquire()


def test_owner_acquisition_cannot_cross_retirement_marker(backend, monkeypatch):
    import agent_runtime.linux_sandbox.lifecycle as lifecycle

    binding = backend.task_binding
    coordinator = RunCoordinator(
        binding.mapping.host_root, binding.task_id, binding.run_id, state_root=binding.state_root
    )
    outcomes, threads = [], []
    original = lifecycle._write

    def acquire():
        try:
            coordinator.acquire()
            outcomes.append("acquired")
        except RuntimeError as exc:
            outcomes.append(str(exc))

    def write(path, record):
        if record.get("state") == "retiring":
            thread = threading.Thread(target=acquire)
            threads.append(thread)
            thread.start()
        original(path, record)

    monkeypatch.setattr(lifecycle, "_write", write)
    backend.retire()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert outcomes == ["sandbox_task_retired"]


def test_permission_issuance_cannot_cross_retirement_marker(backend, monkeypatch):
    import agent_runtime.linux_sandbox.lifecycle as lifecycle

    binding = backend.task_binding
    request = SandboxRequest(
        binding.mapping.workspace_id,
        binding.task_id,
        binding.run_id,
        "racing-approval",
        "command",
        ("/toolchain/bin/python", "-c", "print(1)"),
        output_limit_bytes=65536,
    )
    outcomes, threads = [], []
    original = lifecycle._write

    def authorize():
        try:
            backend.permissions.authorize(request)
            outcomes.append("authorized")
        except ValueError as exc:
            outcomes.append(str(exc))

    def write(path, record):
        if record.get("state") == "retiring":
            thread = threading.Thread(target=authorize)
            threads.append(thread)
            thread.start()
        original(path, record)

    monkeypatch.setattr(lifecycle, "_write", write)
    backend.retire()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert outcomes == ["sandbox_task_retired"]
    assert backend.permissions.snapshot()["tickets"] == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing contract")
def test_atomic_marker_replace_waits_for_short_lived_reader(tmp_path):
    from agent_runtime.linux_sandbox.receipts import _read

    path = tmp_path / "marker.json"
    _write(path, {"state": "retiring"})
    started = threading.Event()
    errors = []

    def write():
        started.set()
        try:
            _write(path, {"state": "retired"})
        except OSError as exc:
            errors.append(exc)

    with path.open("rb"):
        thread = threading.Thread(target=write)
        thread.start()
        assert started.wait(timeout=1)
        thread.join(timeout=0.05)
    thread.join(timeout=2)
    assert not thread.is_alive() and errors == []
    assert _read(path) == {"state": "retired"}


def test_close_cancels_only_own_calls_and_closes_admission(backend):
    event = threading.Event()
    backend._active["call"] = event
    with pytest.raises(ValueError, match="shutdown incomplete"):
        backend.close(timeout_s=0.01)
    assert event.is_set() and backend._closed
    backend._active.clear()
    assert backend.close() is None
    assert backend.close() is None


def test_execution_identity_includes_runtime_call_and_task():
    request = SimpleNamespace(workspace_id="w", task_id="t", run_id="r", call_id="c")
    original = execution_key(request, "a" * 64)
    assert original != execution_key(request, "b" * 64)
    for name in ("workspace_id", "task_id", "run_id", "call_id"):
        assert original != execution_key(
            SimpleNamespace(**{**vars(request), name: "other"}), "a" * 64
        )


def test_dependency_disk_and_inode_budgets_are_enforced(tmp_path, monkeypatch):
    import agent_runtime.linux_sandbox.resource_supervisor as supervisor

    (tmp_path / "data").write_bytes(b"0123456789")
    monkeypatch.setattr(supervisor, "DEPENDENCY_BYTES", 9)
    with pytest.raises(ValueError, match="resource_limit_exceeded"):
        dependency_usage(tmp_path)
    monkeypatch.setattr(supervisor, "DEPENDENCY_BYTES", 1000)
    monkeypatch.setattr(supervisor, "DEPENDENCY_ENTRIES", 1)
    (tmp_path / "second").write_text("x")
    with pytest.raises(ValueError, match="resource_limit_exceeded"):
        dependency_usage(tmp_path)
