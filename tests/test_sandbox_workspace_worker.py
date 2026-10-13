"""Fixed worker logic; these unit tests do not prove OS sandbox isolation."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.workspace_contract import MAX_SOURCE_BYTES, tool_result_from_wire
from agent_runtime.linux_sandbox.workspace_worker import (
    WorkspaceMutation,
    WorkspaceReply,
    execute_request,
)
from agent_runtime.tool_result import ToolResult


def call(root, name, args=None, versions=None):
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name=name,
        tool_args=args or {},
        base_versions=versions or {},
    )
    return execute_request(request, root=root)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", ["write_file", "patch_file", "apply_patch"])
def test_edit_worker_needs_only_standard_library(tmp_path, name):
    path = tmp_path / "a.txt"
    path.write_bytes(b"old\n")
    args = {
        "write_file": {"path": "a.txt", "content": "new\n"},
        "patch_file": {"path": "a.txt", "old_text": "old", "new_text": "new"},
        "apply_patch": {
            "patch": "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"
        },
    }[name]
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name=name,
        tool_args=args,
        base_versions={"a.txt": digest(path)},
    )
    program = (
        "import sys,types,json; from pathlib import Path; "
        "p=types.ModuleType('agent_runtime'); p.__path__=[sys.argv[1]]; "
        "sys.modules['agent_runtime']=p; "
        "from agent_runtime.linux_sandbox.models import SandboxRequest; "
        "from agent_runtime.linux_sandbox.workspace_worker import execute_request; "
        "r=SandboxRequest.from_wire(json.loads(sys.stdin.read())); "
        "print(json.dumps(execute_request(r,root=Path(sys.argv[2])).to_wire()))"
    )
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            program,
            str(Path(__file__).resolve().parents[1] / "agent_runtime"),
            str(tmp_path),
        ],
        input=json.dumps(request.to_wire()),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    reply = WorkspaceReply.from_wire(json.loads(process.stdout), request)
    assert reply.mutations[0].content == "new\n"
    assert path.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("key", ["base_hash", "base_sha256"])
def test_explicit_base_must_match_envelope(tmp_path, key):
    path = tmp_path / "a.txt"
    path.write_bytes(b"old")
    reply = call(
        tmp_path,
        "write_file",
        {"path": "a.txt", "content": "new", key: "f" * 64},
        {"a.txt": digest(path)},
    )
    assert reply.result.error_code == "stale_precondition"
    assert not reply.mutations and path.read_bytes() == b"old"


def test_source_version_enforces_budget_before_read(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_bytes(b"abcd")
    assert call(
        tmp_path, "workspace_source_version", {"path": "a.txt", "max_bytes": 4}
    ).result.metadata["workspace_versions"] == {"a.txt": digest(path)}
    monkeypatch.setattr(type(path), "open", lambda *_a, **_k: pytest.fail("over-budget read"))
    result = call(tmp_path, "workspace_source_version", {"path": "a.txt", "max_bytes": 3}).result
    assert result.error_code == "output_too_large"


def test_missing_source_version_requires_explicit_absence_probe(tmp_path):
    assert not call(tmp_path, "workspace_source_version", {"path": "pkg/new.py"}).result.ok
    reply = call(
        tmp_path, "workspace_source_version", {"path": "pkg/new.py", "allow_missing": True}
    )
    assert reply.result.ok
    assert reply.result.metadata["workspace_versions"] == {"pkg/new.py": None}
    assert not (tmp_path / "pkg").exists()
    (tmp_path / "existing.py").write_bytes(b"pass\n")
    reply = call(
        tmp_path, "workspace_source_version", {"path": "existing.py", "allow_missing": True}
    )
    assert reply.result.metadata["workspace_versions"] == {
        "existing.py": digest(tmp_path / "existing.py")
    }


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "calculator.py").write_text(
        "def add(a: int, b: int):\n    return a + b\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_calculator.py").write_text(
        "def test_add():\n    assert True\n",
        encoding="utf-8",
    )
    return tmp_path


def test_read_preserves_bounded_retrieval_and_sandbox_version(workspace):
    reply = call(workspace, "read_file", {"path": "src/calculator.py"})
    result = tool_result_from_wire(reply.to_wire()["tool_result"])
    assert result.ok
    assert "return a + b" in result.content
    assert result.metadata["retrieval_result"]["query_type"] == "range_read"
    assert result.metadata["workspace_versions"] == {
        "src/calculator.py": digest(workspace / "src/calculator.py")
    }
    assert result.metadata["read_version"]["path"] == "src/calculator.py"
    assert result.metadata["read_version"]["authority"] == "sandbox_workspace"
    assert reply.mutations == []


def test_search_and_parse_never_spawn_or_import_project_code(workspace, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("workspace worker spawned a command")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    (workspace / "src/calculator.py").write_text(
        "raise RuntimeError('project must never execute')\nasync def add(a: int):\n    return a\n",
        encoding="utf-8",
    )
    parsed = call(workspace, "ast_parse", {"path": "src/calculator.py"}).result
    assert parsed.ok
    assert json.loads(parsed.content)[0]["type"] == "async_function"
    listed = call(workspace, "list_files", {"path": "src"}).result
    assert listed.ok and "calculator.py" in listed.content
    searched = call(workspace, "grep", {"pattern": "async def", "path": "src"}).result
    assert searched.ok and "async def add" in searched.content


def test_inspect_combines_read_and_structure_evidence(workspace):
    result = call(workspace, "inspect_file", {"path": "src/calculator.py"}).result
    assert result.ok
    assert "# AST" in result.content
    assert '"name": "add"' in result.content
    assert result.metadata["workspace_versions"]["src/calculator.py"] == digest(
        workspace / "src/calculator.py"
    )


def test_find_test_is_bounded_and_preserves_evidence(workspace):
    result = call(
        workspace, "find_test", {"file_path": "src/calculator.py", "function_name": "add"}
    ).result
    assert result.ok
    matches = json.loads(result.content)
    assert matches[0]["strategy"] == "filename_match"
    assert matches[1]["test_function"] == "test_add"
    assert result.metadata["search_complete"]
    assert result.metadata["source_dependencies"]["tests/test_calculator.py"] == digest(
        workspace / "tests/test_calculator.py"
    )


def test_metadata_reads_only_allowlisted_docs_and_does_not_invent_git(workspace):
    (workspace / "AGENTS.md").write_text("project instructions", encoding="utf-8")
    (workspace / ".git").write_text("gitdir: external-secret", encoding="utf-8")
    result = call(workspace, "workspace_metadata").result
    assert result.ok
    assert result.data == {
        "doc_contents": {"AGENTS.md": "project instructions"},
        "git_available": False,
    }
    assert ".git" not in result.metadata["source_dependencies"]
    assert not call(workspace, "workspace_metadata", {"path": ".git"}).result.ok


@pytest.mark.parametrize("name", ["write_file", "patch_file"])
def test_existing_file_requires_fresh_base_before_planning(workspace, name):
    args = {
        "path": "src/calculator.py",
        "content": "x = 1\n",
        "old_text": "a + b",
        "new_text": "a - b",
    }
    before = (workspace / "src/calculator.py").read_bytes()
    for versions in ({}, {"src/calculator.py": "0" * 64}):
        reply = call(workspace, name, args, versions)
        assert reply.result.error_code in {"stale_precondition", "stale_preimage"}
        assert reply.mutations == []
        assert (workspace / "src/calculator.py").read_bytes() == before


def test_write_and_append_only_prepare_conditional_mutation(workspace):
    path = workspace / "src/calculator.py"
    before = path.read_bytes().decode("utf-8")
    base = digest(path)
    reply = call(
        workspace,
        "write_file",
        {"path": "src/calculator.py", "content": "\n# added\n", "append": True},
        {"src/calculator.py": base},
    )
    assert reply.result.status == "dry_run"
    assert reply.result.changed_files == []
    assert reply.mutations[0].base_sha256 == base
    assert reply.mutations[0].content == before + "\n# added\n"
    assert path.read_bytes().decode("utf-8") == before


def test_new_file_requires_explicit_nonexistent_base(workspace):
    reply = call(
        workspace, "write_file", {"path": "new.py", "content": "x = 1\n"}, {"new.py": None}
    )
    assert reply.result.status == "dry_run"
    assert reply.mutations[0].base_sha256 is None
    assert not (workspace / "new.py").exists()
    reply = call(workspace, "write_file", {"path": "new.py", "content": "x = 1\n"})
    assert reply.result.error_code == "stale_precondition"


def test_patch_preserves_user_bytes_and_rejects_ambiguous_preimage(workspace):
    path = workspace / "src/calculator.py"
    reply = call(
        workspace,
        "patch_file",
        {"path": "src/calculator.py", "old_text": "a + b", "new_text": "a - b"},
        {"src/calculator.py": digest(path)},
    )
    assert reply.result.status == "dry_run"
    assert "a - b" in reply.mutations[0].content
    assert "a + b" in path.read_text(encoding="utf-8")
    bad = call(
        workspace,
        "patch_file",
        {"path": "src/calculator.py", "old_text": "a", "new_text": "c"},
        {"src/calculator.py": digest(path)},
    )
    assert bad.result.error_code == "stale_preimage"
    assert bad.mutations == []


def test_multi_file_patch_validates_every_file_before_returning_plan(workspace):
    patch = "*** Begin Patch\n*** Add File: new.py\n+x = 1\n*** Update File: src/calculator.py\n@@\n-    return wrong\n+    return a - b\n*** End Patch"
    path = workspace / "src/calculator.py"
    before = path.read_bytes()
    reply = call(
        workspace,
        "apply_patch",
        {"patch": patch},
        {"new.py": None, "src/calculator.py": digest(path)},
    )
    assert reply.result.error_code == "stale_preimage"
    assert reply.mutations == []
    assert path.read_bytes() == before
    assert not (workspace / "new.py").exists()


def test_apply_patch_handles_add_delete_and_update_without_side_effects(workspace):
    patch = "*** Begin Patch\n*** Add File: new.py\n+x = 1\n*** Update File: src/calculator.py\n@@\n-    return a + b\n+    return a - b\n*** Delete File: tests/test_calculator.py\n*** End Patch"
    reply = call(
        workspace,
        "apply_patch",
        {"patch": patch},
        {
            "new.py": None,
            "src/calculator.py": digest(workspace / "src/calculator.py"),
            "tests/test_calculator.py": digest(workspace / "tests/test_calculator.py"),
        },
    )
    assert reply.result.status == "dry_run"
    assert [item.path for item in reply.mutations] == [
        "new.py",
        "src/calculator.py",
        "tests/test_calculator.py",
    ]
    assert reply.mutations[-1].content is None
    assert not (workspace / "new.py").exists()
    assert (workspace / "tests/test_calculator.py").exists()


def test_multiple_hunks_use_original_coordinates(workspace):
    path = workspace / "values.txt"
    path.write_bytes(b"a\nb\nc\nd\ne\n")
    diff = "@@ -1,1 +1,2 @@\n-a\n+A\n+extra\n@@ -4,1 +5,1 @@\n-d\n+D\n"
    reply = call(
        workspace, "patch_file", {"path": "values.txt", "diff": diff}, {"values.txt": digest(path)}
    )
    assert reply.result.status == "dry_run"
    assert reply.mutations[0].content == "A\nextra\nb\nc\nD\ne\n"


@pytest.mark.parametrize(
    "name,args,versions",
    [
        ("write_file", {"path": "new.py", "content": "def broken(:"}, {"new.py": None}),
        ("apply_patch", {"patch": "*** Add File: ../escape.py\n+x = 1"}, {"escape.py": None}),
        (
            "write_file",
            {"path": "src/calculator.py", "content": "x = 1"},
            {"src/calculator.py": None},
        ),
        ("read_file", {"path": ".git"}, {}),
        ("ast_parse", {"path": ".agent/state"}, {}),
        ("find_test", {"file_path": "../outside", "function_name": "add"}, {}),
        ("code_lookup", {"path": "src/calculator.py"}, {}),
        ("run_shell", {"command": "whoami"}, {}),
    ],
)
def test_invalid_unported_or_protected_requests_never_mutate(workspace, name, args, versions):
    reply = call(workspace, name, args, versions)
    assert not reply.result.ok
    assert reply.mutations == []
    assert not (workspace / "new.py").exists()


def test_no_change_has_no_mutation_plan(workspace):
    path = workspace / "src/calculator.py"
    reply = call(
        workspace,
        "write_file",
        {"path": "src/calculator.py", "content": path.read_bytes().decode("utf-8")},
        {"src/calculator.py": digest(path)},
    )
    assert reply.result.status == "no_change"
    assert reply.mutations == []


def test_ast_source_limit(workspace):
    (workspace / "large.py").write_bytes(b"#" * (MAX_SOURCE_BYTES + 1))
    result = call(workspace, "ast_parse", {"path": "large.py"}).result
    assert result.error_code == "output_too_large"


def test_hardlinks_do_not_supply_write_authority(workspace):
    source = workspace / "src/calculator.py"
    os.link(source, workspace / "alias.py")
    reply = call(workspace, "read_file", {"path": "alias.py"})
    assert reply.result.error_code == "policy_denied"
    assert "workspace_versions" not in reply.result.metadata


def test_unknown_protocol_operation_is_rejected(workspace):
    request = SandboxRequest("ws", "task", "run", "call", "command", ("python",))
    assert execute_request(request, root=workspace).result.error_code == "policy_denied"
    assert execute_request(replace(request, operation=[]), root=workspace).result.failed


def test_reply_limit_discards_entire_mutation_plan(workspace):
    reply = WorkspaceReply(
        ToolResult(status="dry_run"), [WorkspaceMutation("new.txt", None, "中" * 180000)]
    )
    wire = reply.to_wire()
    assert wire["mutations"] == []
    assert tool_result_from_wire(wire["tool_result"]).error_code == "output_too_large"


def test_patch_keeps_crlf_and_does_not_normalize_untouched_bytes(workspace):
    path = workspace / "values.txt"
    before = b"a\r\nb\r\nc\r\n"
    path.write_bytes(before)
    reply = call(
        workspace,
        "patch_file",
        {
            "path": "values.txt",
            "diff": "@@ -2,1 +2,1 @@\n-b\n+B\n",
        },
        {"values.txt": digest(path)},
    )
    assert reply.mutations[0].content.encode("utf-8") == b"a\r\nB\r\nc\r\n"
    assert path.read_bytes() == before


def test_read_race_does_not_mark_file_as_observed(workspace, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_worker as worker

    original = worker.read_file_result

    def raced(context, args):
        result = original(context, args)
        (workspace / "src/calculator.py").write_text("changed = True\n", encoding="utf-8")
        return result

    monkeypatch.setattr(worker, "read_file_result", raced)
    result = call(workspace, "read_file", {"path": "src/calculator.py"}).result
    assert result.error_code == "stale_precondition"
    assert "workspace_versions" not in result.metadata
