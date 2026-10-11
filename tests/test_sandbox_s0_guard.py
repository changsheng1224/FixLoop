"""The direct-workspace broker must resist aliases and concurrent replacement."""

import errno
import os
import subprocess

import pytest

from scripts import wsl_sandbox_s0_probe as probe
from scripts.wsl_sandbox_s0_guard import FuseView, WindowsBroker, validate_path


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "a/../x",
        "/absolute",
        "a\\x",
        ".ENV",
        ".AGENT/x",
        ".Git/config",
        "a.",
        "a ",
        "x:stream",
        "NUL",
        "con.txt",
        "a\x00b",
    ],
)
def test_ambiguous_or_protected_paths_are_denied(path):
    with pytest.raises(PermissionError):
        validate_path(path)


@pytest.fixture
def broker(tmp_path):
    if os.name != "nt":
        pytest.skip("native Windows file identity and share-mode tests")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source.txt").write_text("original")
    (root / ".env").write_text("secret")
    service = WindowsBroker(root)
    try:
        yield service, root
    finally:
        service.close()


def test_direct_read_write_and_unicode_create(broker):
    service, root = broker
    import base64

    service.call({"op": "create", "path": "中文 file.txt"})
    service.call(
        {
            "op": "write",
            "path": "中文 file.txt",
            "offset": 0,
            "data": base64.b64encode(b"persist").decode(),
        }
    )
    assert (root / "中文 file.txt").read_text() == "persist"
    assert (
        base64.b64decode(
            service.call({"op": "read", "path": "source.txt", "offset": 0, "size": 100})
        )
        == b"original"
    )


def test_protected_aliases_and_hardlinks_are_denied(broker, tmp_path):
    service, root = broker
    outside = tmp_path / "external"
    outside.write_text("outside")
    os.link(outside, root / "outside-alias")
    os.link(root / ".env", root / "secret-alias")
    for path in (".ENV", "secret-alias", "outside-alias"):
        with pytest.raises(PermissionError):
            service.call({"op": "read", "path": path, "offset": 0, "size": 100})
        with pytest.raises(PermissionError):
            service.call({"op": "truncate", "path": path, "size": 0})
    assert outside.read_text() == "outside"
    assert (root / ".env").read_text() == "secret"


def test_link_race_during_write_preserves_external_inode(broker, monkeypatch, tmp_path):
    service, root = broker
    source = root / "source.txt"
    outside = tmp_path / "racing-link"
    native_read = service.api.ReadFile

    def race(*args):
        if not outside.exists():
            os.link(source, outside)
        return native_read(*args)

    monkeypatch.setattr(service.api, "ReadFile", race)
    import base64

    service.call(
        {
            "op": "write",
            "path": "source.txt",
            "offset": 0,
            "data": base64.b64encode(b"replaced").decode(),
        }
    )
    assert source.read_text() == "replaced"
    assert outside.read_text() == "original"
    assert not list(root.glob(".fixloop-s0-private-*"))


def test_reparse_directory_is_denied_without_touching_target(broker, tmp_path):
    service, root = broker
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "sentinel").write_text("outside")
    link = root / "junction"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input="$ErrorActionPreference = 'Stop'\n"
        + f"New-Item -ItemType Junction -Path '{link}' -Target '{outside}' | Out-Null\n",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert link.is_dir(), result.stderr
    try:
        with pytest.raises(PermissionError):
            service.call({"op": "truncate", "path": "junction/sentinel", "size": 0})
        assert (outside / "sentinel").read_text() == "outside"
    finally:
        link.rmdir()


def test_rename_and_delete_preserve_protected_state(broker):
    service, root = broker
    service.call({"op": "rename", "path": "source.txt", "destination": "renamed.txt"})
    assert (root / "renamed.txt").read_text() == "original"
    with pytest.raises(PermissionError):
        service.call({"op": "rename", "path": "renamed.txt", "destination": ".ENV"})
    service.call({"op": "unlink", "path": "renamed.txt"})
    assert not (root / "renamed.txt").exists()
    assert (root / ".env").read_text() == "secret"


def test_unknown_operation_fails_closed(broker):
    service, _ = broker
    with pytest.raises(OSError) as caught:
        service.call({"op": "symlink", "path": "source.txt"})
    assert caught.value.errno == errno.EPERM


def test_staging_file_cannot_be_linked_while_data_is_written(broker, monkeypatch):
    import base64

    service, root = broker
    native_write = service.api.WriteFile
    attempts = []

    def race(*args):
        (temporary,) = root.glob(".fixloop-s0-private-*")
        with pytest.raises(OSError):
            os.link(temporary, root / "staging-alias")
        attempts.append(True)
        return native_write(*args)

    monkeypatch.setattr(service.api, "WriteFile", race)
    service.call(
        {
            "op": "write",
            "path": "source.txt",
            "offset": 0,
            "data": base64.b64encode(b"changed!").decode(),
        }
    )
    assert attempts
    assert (root / "source.txt").read_text() == "changed!"
    assert not (root / "staging-alias").exists()


def test_failed_write_removes_staging_and_preserves_source(broker, monkeypatch):
    import base64

    service, root = broker

    def fail(*args):
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr(service.api, "WriteFile", fail)
    with pytest.raises(OSError, match="disk full"):
        service.call(
            {
                "op": "write",
                "path": "source.txt",
                "offset": 0,
                "data": base64.b64encode(b"changed").decode(),
            }
        )
    assert (root / "source.txt").read_text() == "original"
    assert not list(root.glob(".fixloop-s0-private-*"))


def test_pinned_parent_cannot_be_replaced_by_junction(broker, tmp_path):
    service, root = broker
    parent = root / "parent"
    parent.mkdir()
    with service.parents("parent/file.txt"):
        with pytest.raises(OSError):
            parent.rename(root / "moved-parent")
    assert parent.is_dir()


def test_truncate_is_persistent_and_creates_a_new_file_identity(broker):
    service, root = broker
    source = root / "source.txt"
    original = source.stat().st_ino
    service.call({"op": "truncate", "path": "source.txt", "size": 3})
    assert source.read_text() == "ori"
    assert source.stat().st_ino != original


def test_write_crossing_file_limit_is_denied_before_mutation(broker):
    service, root = broker
    with pytest.raises(OSError) as caught:
        service.call({"op": "write", "path": "source.txt", "offset": 1 << 28, "data": "eA=="})
    assert caught.value.errno == errno.EFBIG
    assert (root / "source.txt").read_text() == "original"
    assert not list(root.glob(".fixloop-s0-private-*"))


def test_staging_alias_before_delete_pending_causes_write_denial(broker, monkeypatch):
    import base64

    service, root = broker
    native_disposition = service._disposition
    linked = False

    def race(handle, pending):
        nonlocal linked
        if pending and not linked:
            (temporary,) = root.glob(".fixloop-s0-private-*")
            os.link(temporary, root / "staging-alias")
            linked = True
        return native_disposition(handle, pending)

    monkeypatch.setattr(service, "_disposition", race)
    with pytest.raises(PermissionError):
        service.call(
            {
                "op": "write",
                "path": "source.txt",
                "offset": 0,
                "data": base64.b64encode(b"changed").decode(),
            }
        )
    assert (root / "source.txt").read_text() == "original"
    assert (root / "staging-alias").read_bytes() == b""


def test_stdio_broker_protocol_handles_denial_then_terminal_report(broker):
    import json
    import sys

    _, root = broker
    code = """import json,sys
print(json.dumps({'rpc': {'op': 'read', 'path': '.ENV', 'offset': 0, 'size': 10}}), flush=True)
assert json.loads(sys.stdin.readline())['errno'] == 13
print(json.dumps({'schema': 'terminal-report', 'denied': True}), flush=True)
"""
    result = probe.broker_worker([sys.executable, "-I", "-c", code], root)
    assert result.returncode == 0
    assert json.loads(result.stdout)["denied"] is True


def test_stdio_broker_cannot_accept_duplicate_terminal_reports(broker):
    import sys

    _, root = broker
    code = "print('{}'); print('{}')"
    with pytest.raises(ValueError, match="duplicate"):
        probe.broker_worker([sys.executable, "-I", "-c", code], root)


def test_incomplete_positive_worker_report_is_rejected():
    import json

    report = {"schema": probe.SCHEMA, "checks": {"supported_environment": {"passed": True}}}
    with pytest.raises(ValueError, match="missing Linux evidence"):
        probe.worker_evidence(json.dumps(report), guarded=True)
    report["checks"]["supported_environment"]["passed"] = False
    assert (
        probe.worker_evidence(json.dumps(report), guarded=True)["checks"]["supported_environment"][
            "passed"
        ]
        is False
    )


def test_guarded_target_cannot_omit_additional_attack_checks():
    import json

    checks = {name: {"passed": True} for name in probe.TARGET_CHECKS}
    with pytest.raises(ValueError, match="evidence"):
        probe.target_evidence(json.dumps(checks), guarded=True)


def test_fuse_statfs_reply_has_complete_kernel_structure():
    import struct

    view = FuseView(None, "workspace")
    reply = view.dispatch(17, 1, b"")
    # Linux fuse_kstatfs: five u64 counters, four u32 fields, six spare u32.
    assert len(reply) == 80
    fields = struct.unpack("<5Q10I", reply)
    assert fields[5:8] == (4096, 255, 4096)
    assert fields[8:] == (0,) * 7
