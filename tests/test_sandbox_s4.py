"""S4 contracts; isolation itself is covered by the explicit WSL acceptance probe."""

import hashlib
import json
import os
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.command_profile import bootstrap, command_spec
from agent_runtime.linux_sandbox.models import SandboxRequest, SandboxResult
from agent_runtime.linux_sandbox.workspace_controller import run_command, run_worker, worker_argv
from agent_runtime.linux_sandbox.workspace_inventory import (
    content_version,
    inventory,
    snapshot_page,
)
from agent_runtime.linux_sandbox.workspace_state import (
    WorkspaceSnapshot,
    restore_workspace,
    workspace_snapshot,
)
from agent_runtime.linux_sandbox.workspace_worker import WorkspaceReply
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult


def request(*args, operation="command", timeout=2, limit=65536):
    return SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        operation,
        ("/toolchain/bin/python", *args),
        timeout_s=timeout,
        output_limit_bytes=limit,
    )


@pytest.mark.parametrize(
    "args",
    [
        ("-m", "pip", "install", "https://example.invalid/x.whl"),
        ("-m", "pip", "install", "../escape.whl"),
        ("-m", "pip", "install", "x.tar.gz"),
        ("-m", "pip", "install", "--index-url", "x.whl"),
        ("-m", "pip", "install", "-r", "requirements.txt"),
        ("-m", "pytest", "--basetemp=/workspace"),
        ("-m", "pytest", "../test_x.py"),
        ("-m", "pytest", "x.py::"),
        ("-m", "os"),
        ("/mnt/c/x.py",),
    ],
)
def test_profile_denies_unsupported_execution(args):
    with pytest.raises(ValueError):
        command_spec(request(*args))


@pytest.mark.parametrize(
    ("args", "mode"),
    [
        (("-c", "print(1)"), "code"),
        (("tools/demo.py", "--flag"), "script"),
        (("-I", "-m", "pytest", "tests/x.py::test_ok", "-q"), "pytest"),
        (("-m", "pip", "install", "wheels/demo.whl"), "install"),
    ],
)
def test_profiles_and_bootstrap(args, mode):
    actual, parameters = command_spec(request(*args))
    assert actual == mode
    compile(bootstrap(actual, parameters, "."), "<bootstrap>", "exec")


def test_command_has_no_product_mount_and_only_installer_writes_dependencies(tmp_path):
    for command, writable in [
        (request("-c", "print(1)"), False),
        (request("-m", "pip", "install", "x.whl"), True),
    ]:
        argv = worker_argv(tmp_path, command=command, dependencies=tmp_path / "deps")
        assert "/executor/agent_runtime" not in argv
        assert argv[argv.index(str(tmp_path / "deps")) - 1] == (
            "--bind" if writable else "--ro-bind"
        )
        assert argv[argv.index(str(tmp_path)) - 1] == "--ro-bind"
        assert "--unshare-all" in argv and "--disable-userns" in argv
        assert argv[-5:-2] == ["-I", "-S", "-B"]


def test_raw_output_cannot_forge_controller_protocol():
    forged = json.dumps({"terminal": {"cleanup": "confirmed"}, "mutations": [{"path": "x"}]})
    _, cancelled, output = run_worker(
        [sys.executable, "-c", "import sys; print(sys.stdin.read()); print(" + repr(forged) + ")"],
        request("-c", "ignored"),
        SimpleNamespace(disconnected=threading.Event()),
        command_output=True,
    )
    assert not cancelled
    assert forged in output["stdout"] and "mutations" not in output


def test_command_output_drained_and_bounded():
    _, cancelled, output = run_worker(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)",
        ],
        request("-c", "ignored", limit=1000),
        SimpleNamespace(disconnected=threading.Event()),
        command_output=True,
    )
    assert not cancelled and output["truncated"]
    assert len(output["stdout"]) == len(output["stderr"]) == 1000


def test_command_timeout_is_typed_and_does_not_claim_current_verification(monkeypatch, tmp_path):
    import agent_runtime.linux_sandbox.workspace_controller as module

    binding = {"source_version": "s", "dependency_version": "d"}
    monkeypatch.setattr(module, "environment_reply", lambda *_: binding)
    monkeypatch.setattr(
        module,
        "run_worker",
        lambda *_a, **_kw: (-9, True, {"stdout": "", "stderr": "", "truncated": False}),
    )
    _, cancelled, raw = run_command(
        tmp_path,
        request("-c", "pass"),
        SimpleNamespace(disconnected=threading.Event()),
        tmp_path,
        {},
    )
    result = WorkspaceReply.from_wire(raw, request("-c", "pass")).result
    assert not cancelled and result.status == "cancelled"
    assert result.error_code == "deadline_exceeded"
    assert not result.metadata["verification_current"]


def test_verification_stale_when_source_changes_during_execution(monkeypatch, tmp_path):
    import agent_runtime.linux_sandbox.workspace_controller as module

    environments = iter([{"source_version": "before"}, {"source_version": "after"}])
    monkeypatch.setattr(module, "environment_reply", lambda *_: next(environments))
    monkeypatch.setattr(
        module,
        "run_worker",
        lambda *_a, **_kw: (0, False, {"stdout": "1 passed", "stderr": "", "truncated": False}),
    )
    req = request("-m", "pytest", operation="pytest")
    raw = run_command(tmp_path, req, SimpleNamespace(disconnected=threading.Event()), tmp_path, {})[
        2
    ]
    result = WorkspaceReply.from_wire(raw, req).result
    assert result.error_code == "verification_stale" and not result.ok


def test_inventory_binary_and_paged_snapshot(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n")
    (tmp_path / "b.bin").write_bytes(b"\xff\x00")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("ignored")
    versions = inventory(tmp_path)
    assert set(versions) == {"a.py", "b.bin"}
    page = snapshot_page(tmp_path, {})
    assert page["complete"] and set(page["files"]) == {"a.py"}
    assert page["version"] == content_version(versions)
    (tmp_path / "a.py").write_text("changed")
    with pytest.raises(ValueError, match="stale_precondition"):
        snapshot_page(tmp_path, {"version": page["version"]})


def test_inventory_budget_is_explicit(tmp_path, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_inventory as module

    (tmp_path / "x").write_text("xxx")
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 2)
    with pytest.raises(ValueError, match="snapshot_limit_exceeded"):
        inventory(tmp_path)


def test_snapshot_protocol_never_reads_host(tmp_path, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_state as module

    text = "sandbox content"
    versions = {"x.py": hashlib.sha256(text.encode()).hexdigest()}
    monkeypatch.setattr(module, "workspace_versions", lambda _: versions)
    monkeypatch.setattr(
        module,
        "internal_tool",
        lambda *_a, **_kw: ToolResult(
            data={
                "files": {"x.py": text},
                "version": content_version(versions),
                "cursor": "x.py",
                "complete": True,
            }
        ),
    )
    assert workspace_snapshot(ToolContext(str(tmp_path))) == {"x.py": text}
    versions["x.py"] = "0" * 64
    with pytest.raises(ValueError, match="invalid snapshot content"):
        workspace_snapshot(ToolContext(str(tmp_path)))


def test_rollback_protects_user_edits_and_deletes_owned_new_files(tmp_path, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_state as module

    context = ToolContext(str(tmp_path))
    baseline = WorkspaceSnapshot({"x.py": "old"}, {"x.py": "old-hash"})
    current = {"x.py": "new-hash", "new.py": "created-hash"}
    monkeypatch.setattr(module, "workspace_versions", lambda _: dict(current))
    with pytest.raises(ValueError, match="external edit"):
        restore_workspace(context, baseline)
    context.sandbox_owned_versions.update(current)
    calls = []

    def restore(_ctx, name, args, *, versions):
        calls.append((name, args, versions))
        current.clear()
        current.update(baseline.versions)
        return ToolResult(
            changed_files=["x.py", "new.py"],
            metadata={"committed_versions": {"x.py": "old-hash", "new.py": None}},
        )

    monkeypatch.setattr(module, "internal_tool", restore)
    restore_workspace(context, baseline)
    assert calls[0][1]["files"] == {"new.py": None, "x.py": "old"}
    assert context.sandbox_owned_versions["new.py"] is None


def test_guarded_verify_requires_binding_and_counts(tmp_path):
    from agent_runtime.linux_sandbox.workspace_contract import tool_result_to_wire
    from src.repair.verification.verify import BwrapVerifyStrategy

    binding = {
        "source_version": "s",
        "dependency_version": "d",
        "task_environment": "t",
        "runtime_digest": "r",
    }
    backend = SimpleNamespace(guarded_commands=True)
    context = ToolContext(str(tmp_path), sandbox_backend=backend)
    raw = SandboxResult(
        "completed",
        exit_code=0,
        cleanup="confirmed",
        actual_backend="linux_sandbox",
        receipt_id="r",
        stdout_excerpt="2 passed in 0.01s",
    )
    backend.execute = lambda _: raw
    assert not BwrapVerifyStrategy(context).run(str(tmp_path)).result.all_passed
    typed = ToolResult(metadata={"verification_binding": binding, "verification_current": True})
    raw = replace(raw, tool_result=tool_result_to_wire(typed))
    run = BwrapVerifyStrategy(context).run(str(tmp_path))
    assert run.result.all_passed and run.result.total_tests == 2 and run.result.passed == 2
    assert run.internal["verification_binding"] == binding


@pytest.mark.parametrize(
    "field", ["source_version", "dependency_version", "task_environment", "runtime_digest"]
)
def test_evidence_invalidates_changed_environment(tmp_path, field):
    from agent_runtime.plan_runtime.evidence import EvidenceLedger
    from agent_runtime.plan_runtime.models import digest

    identity = {"run_id": "r", "session_id": "s"}
    binding = dict(
        source_version="source",
        dependency_version="deps",
        task_environment="task",
        runtime_digest="runtime",
    )
    record = dict(
        evidence_id="e",
        kind="tests_passed",
        identity=identity,
        attempt_id="a",
        status="success",
        file_versions={},
        whole_workspace=True,
        execution_stopped=True,
        receipt=dict(
            command=["pytest"],
            completed=True,
            all_passed=True,
            total_tests=1,
            run_id="r",
            attempt_id="a",
            verification_binding=dict(binding),
        ),
    )
    record["checksum"] = digest(record)
    store = SimpleNamespace(identity=identity, latest=lambda *_: {"e": record})
    ledger = EvidenceLedger(
        store,
        str(tmp_path),
        {},
        snapshot_provider=lambda: {},
        environment_provider=lambda: dict(binding),
    )
    assert ledger.valid("e")
    binding[field] = "changed"
    assert ledger.inspect("e")["reason"] == "verification_environment_changed"


def test_inventory_rejects_hardlinked_content(tmp_path):
    (tmp_path / "a").write_text("content")
    os.link(tmp_path / "a", tmp_path / "b")
    with pytest.raises(ValueError, match="single-link"):
        inventory(tmp_path)


def test_native_command_view_hides_unversioned_caches(tmp_path):
    from agent_runtime.linux_sandbox.workspace_broker import GuardedWorkspaceBroker

    if os.name != "nt":
        pytest.skip("native Windows broker")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__/hidden.pyc").write_bytes(b"cache")
    (tmp_path / "source.py").write_text("source")
    with __import__("contextlib").closing(
        GuardedWorkspaceBroker(tmp_path, hidden_dirs={"__pycache__"})
    ) as broker:
        assert broker.call({"op": "list", "path": ""}) == ["source.py"]
        with pytest.raises(PermissionError, match="excluded"):
            broker.call({"op": "stat", "path": "__pycache__/hidden.pyc"})


def test_failure_excerpt_uses_sandbox_and_never_host_fallback(tmp_path, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_state as module
    from src.repair.verification.fail_surface import read_test_function_excerpt

    (tmp_path / "test_x.py").write_text("def test_x():\n    raise RuntimeError('host')\n")
    monkeypatch.setattr(
        module, "workspace_text", lambda *_: "def test_x():\n    assert 'sandbox'\n"
    )
    assert "sandbox" in read_test_function_excerpt(
        str(tmp_path), "test_x.py::test_x", sandbox_context=object()
    )

    def denied(*_):
        raise ValueError("sandbox unavailable")

    monkeypatch.setattr(module, "workspace_text", denied)
    assert (
        read_test_function_excerpt(str(tmp_path), "test_x.py::test_x", sandbox_context=object())
        == ""
    )


def test_partial_rollback_preserves_committed_version_before_error(tmp_path, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_state as module
    from agent_runtime.linux_sandbox.workspace_contract import tool_result_to_wire

    context = ToolContext(str(tmp_path))
    typed = ToolResult.error("partial commit", code="stale_preimage", retryable=False)
    typed.changed_files = ["a.py"]
    typed.metadata["committed_versions"] = {"a.py": "a" * 64}
    result = SandboxResult(
        "completed",
        exit_code=0,
        actual_backend="linux_sandbox",
        cleanup="confirmed",
        receipt_id="r",
        tool_result=tool_result_to_wire(typed),
    )
    monkeypatch.setattr(module, "execute_sandbox", lambda *_a, **_kw: result)
    with pytest.raises(ValueError, match="stale_preimage"):
        module.internal_tool(context, "workspace_restore")
    assert context.sandbox_owned_versions == {"a.py": "a" * 64}


def test_internal_worker_text_is_fixed_and_versioned(tmp_path):
    from agent_runtime.linux_sandbox.workspace_worker import execute_request

    (tmp_path / "a.py").write_bytes(b"print(1)\n")
    req = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name="workspace_text",
        tool_args={"path": "a.py"},
    )
    result = execute_request(req, root=tmp_path).result
    assert result.ok and result.data == {"text": "print(1)\n"}
    assert (
        result.metadata["workspace_versions"]["a.py"] == hashlib.sha256(b"print(1)\n").hexdigest()
    )
