"""The same inputs produce identical bytes and failures across patch backends."""

import hashlib

import pytest

from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.workspace_worker import execute_request
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_apply_patch
from agent_runtime.workspace_patches import HostPatchTransaction


def apply_backend(root, patch, backend):
    if backend == "host":
        return tool_apply_patch(ToolContext(str(root)), {"patch": patch})
    versions = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    from agent_runtime.apply_patch_format import parse_apply_patch_text

    versions = {op.path: versions.get(op.path) for op in parse_apply_patch_text(patch)}
    request = SandboxRequest(
        "workspace",
        "task",
        "run",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name="apply_patch",
        tool_args={"patch": patch},
        base_versions=versions,
    )
    reply = execute_request(request, root=root)
    if reply.mutations:
        transaction = HostPatchTransaction(ToolContext(str(root)).resolve, list(versions))
        transaction.check_hashes(versions)
        transaction.commit({m.path: m.content for m in reply.mutations})
    return reply.result


@pytest.mark.parametrize("backend", ["host", "sandbox"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_original_coordinates_and_source_headers(tmp_path, backend, newline):
    source = newline.join(["--old", "b", "c", "d", ""])
    (tmp_path / "a.txt").write_bytes(source.encode())
    patch = (
        "*** Begin Patch\n*** Update File: a.txt\n"
        "@@ -1 +1,2 @@\n---old\n+++new\n+insert\n"
        "@@ -4 +5 @@\n-d\n+D\n*** End Patch\n"
    )
    result = apply_backend(tmp_path, patch, backend)
    assert not result.failed, result.content
    assert (tmp_path / "a.txt").read_bytes() == newline.join(
        ["++new", "insert", "b", "c", "D", ""]
    ).encode()


@pytest.mark.parametrize("backend", ["host", "sandbox"])
def test_stale_second_file_leaves_every_file_unchanged(tmp_path, backend):
    (tmp_path / "a.txt").write_bytes(b"old\n")
    (tmp_path / "b.txt").write_bytes(b"actual\n")
    patch = (
        "*** Begin Patch\n*** Update File: a.txt\n@@ -1 +1 @@\n-old\n+new\n"
        "*** Update File: b.txt\n@@ -1 +1 @@\n-stale\n+new\n*** End Patch\n"
    )
    assert apply_backend(tmp_path, patch, backend).failed
    assert (tmp_path / "a.txt").read_bytes() == b"old\n"
    assert (tmp_path / "b.txt").read_bytes() == b"actual\n"


@pytest.mark.parametrize("backend", ["host", "sandbox"])
def test_create_empty_delete_and_no_final_newline(tmp_path, backend):
    (tmp_path / "a.txt").write_bytes(b"old")
    (tmp_path / "delete.txt").write_bytes(b"gone")
    patch = (
        "*** Begin Patch\n*** Update File: a.txt\n@@ -1 +1 @@\n-old\n"
        "\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        "*** Add File: empty.txt\n*** Add File: nested/new.txt\n+created\n"
        "*** Delete File: delete.txt\n*** End Patch\n"
    )
    assert not apply_backend(tmp_path, patch, backend).failed
    assert (tmp_path / "a.txt").read_bytes() == b"new"
    assert (tmp_path / "empty.txt").read_bytes() == b""
    assert (tmp_path / "nested/new.txt").read_bytes() == b"created\n"
    assert not (tmp_path / "delete.txt").exists()


def test_commit_failure_restores_written_bytes_and_new_directories(tmp_path, monkeypatch):
    from agent_runtime import workspace_patches

    (tmp_path / "a.txt").write_bytes(b"old\r\n")
    transaction = HostPatchTransaction(
        ToolContext(str(tmp_path)).resolve, ["a.txt", "nested/new.txt", "b.txt"]
    )
    write = workspace_patches.atomic_write_bytes

    def fail_once(path, data, **kwargs):
        if path.name == "b.txt":
            raise OSError("injected disk failure")
        return write(path, data, **kwargs)

    monkeypatch.setattr(workspace_patches, "atomic_write_bytes", fail_once)
    with pytest.raises(OSError, match="injected disk failure"):
        transaction.commit({"a.txt": "new\n", "nested/new.txt": "new\n", "b.txt": "new\n"})
    assert (tmp_path / "a.txt").read_bytes() == b"old\r\n"
    assert not (tmp_path / "nested").exists()
    assert not (tmp_path / "b.txt").exists()


def test_preimage_changed_after_prepare_is_rejected(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"old\n")
    transaction = HostPatchTransaction(ToolContext(str(tmp_path)).resolve, ["a.txt"])
    path.write_bytes(b"external edit\n")
    with pytest.raises(ValueError, match="preimage changed"):
        transaction.commit({"a.txt": "new\n"})
    assert path.read_bytes() == b"external edit\n"


@pytest.mark.parametrize("backend", ["host", "sandbox"])
def test_unified_creation_no_newline_and_delete(tmp_path, backend):
    (tmp_path / "old.txt").write_bytes(b"gone\r\n")
    patch = (
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+created\n"
        "\\ No newline at end of file\n--- a/old.txt\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-gone\n"
    )
    result = apply_backend(tmp_path, patch, backend)
    assert not result.failed, result.content
    assert (tmp_path / "new.txt").read_bytes() == b"created"
    assert not (tmp_path / "old.txt").exists()


@pytest.mark.parametrize("backend", ["host", "sandbox"])
def test_git_metadata_creation_and_deletion_with_index(tmp_path, backend):
    """Git orders `diff --git`, the mode line, then `index`; all three must parse."""
    (tmp_path / "old.txt").write_bytes(b"gone\n")
    patch = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n"
        "+created\n"
        "diff --git a/old.txt b/old.txt\n"
        "deleted file mode 100644\n"
        "index e69de29..0000000\n"
        "--- a/old.txt\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    )
    result = apply_backend(tmp_path, patch, backend)
    assert not result.failed, result.content
    assert (tmp_path / "new.txt").read_bytes() == b"created\n"
    assert not (tmp_path / "old.txt").exists()


def test_git_mode_metadata_must_agree_with_headers():
    """A creation mode line over an existing-file hunk is still rejected."""
    from agent_runtime.patch_engine import PatchError, parse_unified_patch

    patch = (
        "diff --git a/f.txt b/f.txt\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(PatchError, match="metadata disagrees"):
        parse_unified_patch(patch)


def test_git_duplicate_mode_metadata_is_rejected():
    from agent_runtime.patch_engine import PatchError, parse_unified_patch

    patch = (
        "diff --git a/f.txt b/f.txt\n"
        "new file mode 100644\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/f.txt\n"
        "@@ -0,0 +1 @@\n"
        "+x\n"
    )
    with pytest.raises(PatchError, match="duplicate file mode metadata"):
        parse_unified_patch(patch)


@pytest.mark.parametrize("backend", ["host", "sandbox"])
def test_unicode_line_separator_is_source_content(tmp_path, backend):
    (tmp_path / "a.txt").write_bytes("a\u2028b\n".encode())
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\u2028b\n+c\u2028d\n"
    result = apply_backend(tmp_path, patch, backend)
    assert not result.failed, result.content
    assert (tmp_path / "a.txt").read_bytes() == "c\u2028d\n".encode()
