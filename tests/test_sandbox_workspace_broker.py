"""Native broker boundary evidence; not Linux namespace-isolation evidence."""

import base64
import hashlib
import json
import os
from dataclasses import replace

import pytest

from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.workspace_broker import GuardedWorkspaceBroker
from agent_runtime.linux_sandbox.workspace_worker import WorkspaceReply, execute_request


def request(name, args, versions=None):
    return SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name=name,
        tool_args=args,
        base_versions=versions or {},
    )


@pytest.fixture
def broker(tmp_path):
    if os.name != "nt":
        pytest.skip("native Windows broker")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_bytes(b"original")
    service = GuardedWorkspaceBroker(root)
    try:
        yield service, root
    finally:
        service.close()


def plan(root, name, args, versions=None):
    req = request(name, args, versions)
    return req, execute_request(req, root=root)


def test_snapshot_read_denies_concurrent_write_and_releases_on_close(broker):
    service, root = broker
    stat = service.call({"op": "stat", "path": "a.txt"})
    assert stat["identity"] and "mtime_ns" in stat
    with pytest.raises(PermissionError):
        (root / "a.txt").write_bytes(b"racer")
    assert (
        base64.b64decode(service.call({"op": "read", "path": "a.txt", "offset": 0, "size": 20}))
        == b"original"
    )
    assert "a.txt" in service.call({"op": "list", "path": ""})
    service.release_reads()
    (root / "a.txt").write_bytes(b"after release")


def test_close_releases_root_even_if_read_cleanup_raises(broker, monkeypatch):
    service, _ = broker
    service.call({"op": "stat", "path": "a.txt"})
    release = service.release_reads

    def failing_release():
        release()
        raise OSError("read cleanup failed")

    with monkeypatch.context() as patch:
        patch.setattr(service, "release_reads", failing_release)
        with pytest.raises(OSError, match="read cleanup failed"):
            service.close()
    assert service.root_handle is None


@pytest.mark.parametrize("op", ["write", "create", "truncate", "unlink", "rename", "sync"])
def test_view_channel_never_writes(broker, op):
    service, root = broker
    with pytest.raises(PermissionError):
        service.call({"op": op, "path": "a.txt"})
    assert (root / "a.txt").read_bytes() == b"original"


def test_conditional_commit_is_the_only_success_authority(broker):
    service, root = broker
    req, reply = plan(
        root,
        "write_file",
        {"path": "a.txt", "content": "updated"},
        {"a.txt": hashlib.sha256(b"original").hexdigest()},
    )
    assert reply.result.status == "dry_run"
    events = []
    outcome = service.commit(
        req, WorkspaceReply.from_wire(reply.to_wire(), req), record=events.append
    )
    assert outcome.changed_files == ["a.txt"] and not outcome.error_code
    assert (root / "a.txt").read_bytes() == b"updated"
    assert [e["phase"] for e in events] == ["intent", "committed"]
    assert not list(root.glob(".fixloop-private-*"))


def test_commit_rechecks_after_worker_without_overwriting_user(broker):
    service, root = broker
    req, reply = plan(
        root,
        "write_file",
        {"path": "a.txt", "content": "updated"},
        {"a.txt": hashlib.sha256(b"original").hexdigest()},
    )
    (root / "a.txt").write_bytes(b"user update")
    with pytest.raises(ValueError, match="stale_preimage"):
        service.commit(req, reply, record=lambda event: None)
    assert (root / "a.txt").read_bytes() == b"user update"


def test_create_rejects_racing_existing_file(broker):
    service, root = broker
    req, reply = plan(root, "write_file", {"path": "new.txt", "content": "new"}, {"new.txt": None})
    (root / "new.txt").write_bytes(b"racer")
    with pytest.raises(ValueError, match="stale_preimage"):
        service.commit(req, reply, record=lambda event: None)
    assert (root / "new.txt").read_bytes() == b"racer"


def test_all_versions_checked_before_any_commit(broker):
    service, root = broker
    (root / "b.txt").write_bytes(b"second")
    patch = "*** Begin Patch\n*** Delete File: a.txt\n*** Delete File: b.txt\n*** End Patch"
    versions = {
        "a.txt": hashlib.sha256(b"original").hexdigest(),
        "b.txt": hashlib.sha256(b"second").hexdigest(),
    }
    req, reply = plan(root, "apply_patch", {"patch": patch}, versions)
    (root / "b.txt").write_bytes(b"user")
    with pytest.raises(ValueError, match="stale_preimage"):
        service.commit(req, reply, record=lambda event: None)
    assert (root / "a.txt").read_bytes() == b"original"
    assert (root / "b.txt").read_bytes() == b"user"


def test_delete_and_create_commit(broker):
    service, root = broker
    patch = "*** Begin Patch\n*** Delete File: a.txt\n*** Add File: new.txt\n+new\n*** End Patch"
    req, reply = plan(
        root,
        "apply_patch",
        {"patch": patch},
        {"a.txt": hashlib.sha256(b"original").hexdigest(), "new.txt": None},
    )
    outcome = service.commit(req, reply, record=lambda event: None)
    assert outcome.changed_files == ["a.txt", "new.txt"]
    assert not (root / "a.txt").exists()
    assert (root / "new.txt").read_bytes() == b"new\n"


def test_rename_failure_restores_checked_original(broker, monkeypatch):
    service, root = broker
    req, reply = plan(
        root,
        "write_file",
        {"path": "a.txt", "content": "updated"},
        {"a.txt": hashlib.sha256(b"original").hexdigest()},
    )
    native = service._rename
    calls = 0

    def fail(handle, parent, name, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated staging rename failure")
        return native(handle, parent, name, **kwargs)

    monkeypatch.setattr(service, "_rename", fail)
    outcome = service.commit(req, reply, record=lambda event: None)
    assert outcome.error_code == "commit_failed" and not outcome.changed_files
    assert (root / "a.txt").read_bytes() == b"original"
    assert not list(root.glob(".fixloop-private-*"))


def test_racer_at_replace_gap_preserves_user_and_original(broker, monkeypatch):
    service, root = broker
    req, reply = plan(
        root,
        "write_file",
        {"path": "a.txt", "content": "updated"},
        {"a.txt": hashlib.sha256(b"original").hexdigest()},
    )
    native = service._rename

    def race(handle, parent, name, **kwargs):
        if name.startswith(".fixloop-private-original-"):
            native(handle, parent, name, **kwargs)
            (root / "a.txt").write_bytes(b"racer")
            return
        return native(handle, parent, name, **kwargs)

    monkeypatch.setattr(service, "_rename", race)
    outcome = service.commit(req, reply, record=lambda event: None)
    assert outcome.error_code == "execution_uncertain"
    assert (root / "a.txt").read_bytes() == b"racer"
    recovery = root / outcome.recovery_files["a.txt"]
    assert recovery.read_bytes() == b"original"


def test_mutation_receiver_rejects_forged_paths_bases_and_success(tmp_path):
    req = request("write_file", {"path": "a.txt", "content": "new"}, {"a.txt": None})
    reply = execute_request(req, root=tmp_path)
    for field, value in [("path", "other.txt"), ("base_sha256", "0" * 64), ("content", 3)]:
        raw = json.loads(json.dumps(reply.to_wire()))
        raw["mutations"][0][field] = value
        with pytest.raises(ValueError, match="workspace_result_invalid"):
            WorkspaceReply.from_wire(raw, req)
    raw = reply.to_wire()
    raw["tool_result"]["status"] = "success"
    with pytest.raises(ValueError, match="workspace_result_invalid"):
        WorkspaceReply.from_wire(raw, req)
    with pytest.raises(ValueError, match="workspace_result_invalid"):
        WorkspaceReply.from_wire(
            reply.to_wire(), replace(req, tool_name="read_file", base_versions={})
        )


@pytest.mark.parametrize("phase", ["intent", "committed"])
def test_journal_failure_preserves_actual_changes_and_recovery(broker, phase):
    service, root = broker
    req, reply = plan(
        root,
        "write_file",
        {"path": "a.txt", "content": "updated"},
        {"a.txt": hashlib.sha256(b"original").hexdigest()},
    )

    def record(event):
        if event["phase"] == phase:
            raise OSError("journal unavailable")

    outcome = service.commit(req, reply, record=record)
    if phase == "intent":
        assert not outcome.changed_files
        assert (root / "a.txt").read_bytes() == b"original"
    else:
        assert outcome.error_code == "execution_uncertain"
        assert outcome.changed_files == ["a.txt"]
        assert (root / "a.txt").read_bytes() == b"updated"
        assert (root / outcome.recovery_files["a.txt"]).read_bytes() == b"original"
