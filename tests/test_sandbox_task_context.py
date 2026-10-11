"""S1 identity and startup boundaries; no model or production-profile execution."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.routing import execute_sandbox
from agent_runtime.linux_sandbox.task_context import (
    SandboxEnvironmentIdentity,
    SandboxTaskBinding,
    SandboxTaskSpec,
    WorkspaceMapping,
    windows_to_wsl,
)
from agent_runtime.state_root import managed_state_base
from agent_runtime.tool_context import ToolContext
from agent_runtime.worktree import (
    WorktreeError,
    adopt_worktree,
    cleanup_stale_worktrees,
    create_worktree,
    remove_worktree,
    retain_worktree,
)
from src.orchestrator import Orchestrator
from src.repair.checkpoint_load import save_repair_checkpoint
from src.repair.run_context import RepairRunContext
from src.state import RepairState


@pytest.fixture
def spec(tmp_path):
    return SandboxTaskSpec(
        SandboxEnvironmentIdentity("FixLoop-MVP", "1", "1", "a" * 64),
        "s1-v1",
        "b" * 64,
        str(tmp_path / "external"),
    )


def orchestrator(repo, spec, factory=None):
    context = ToolContext(str(repo))
    agent = SimpleNamespace(tool_context=context, _cwd=str(repo), state_root="")
    return Orchestrator(
        agent,
        sandbox_context=context,
        sandbox_task_spec=spec,
        sandbox_backend_factory=factory or (lambda binding: SimpleNamespace(task_binding=binding)),
    )


def metadata_backend(binding):
    """Canonical S3 metadata reply for real-Agent startup in identity tests."""
    from agent_runtime.linux_sandbox.models import SandboxResult
    from agent_runtime.linux_sandbox.workspace_contract import tool_result_to_wire
    from agent_runtime.linux_sandbox.workspace_inventory import content_version
    from agent_runtime.tool_result import ToolResult

    def execute(request):
        assert request.tool_name in {
            "workspace_metadata",
            "workspace_inventory",
            "workspace_snapshot",
        }
        if request.tool_name == "workspace_inventory":
            return SandboxResult(
                "completed", exit_code=0, actual_backend="linux_sandbox", cleanup="confirmed",
                receipt_id=request.call_id, policy_digest=binding.policy_digest,
                tool_result=tool_result_to_wire(ToolResult(
                    "workspace inventory", data={},
                    metadata={"content_version": content_version({})},
                )),
            )
        if request.tool_name == "workspace_snapshot":
            return SandboxResult(
                "completed", exit_code=0, actual_backend="linux_sandbox", cleanup="confirmed",
                receipt_id=request.call_id, policy_digest=binding.policy_digest,
                tool_result=tool_result_to_wire(ToolResult(
                    "workspace snapshot", data={
                        "files": {},
                        "version": content_version({}),
                        "cursor": "",
                        "complete": True,
                    },
                )),
            )
        return SandboxResult(
            "completed", exit_code=0, actual_backend="linux_sandbox", cleanup="confirmed",
            receipt_id=request.call_id, policy_digest=binding.policy_digest,
            tool_result=tool_result_to_wire(ToolResult(
                "workspace metadata", data={"doc_contents": {}, "git_available": False},
                metadata={"workspace_versions": {}, "source_dependencies": {}},
            )),
        )

    return SimpleNamespace(task_binding=binding, execute=execute, cancel=lambda _: False)


def test_windows_mapping_preserves_unicode_and_spaces():
    assert windows_to_wsl(r"C:\项目 空间\仓库") == "/mnt/c/项目 空间/仓库"


@pytest.mark.parametrize(
    "raw",
    [
        r"C:repo",
        r"\\server\share\repo",
        r"C:\repo\..\other",
        r"C:\repo\a:stream",
        "C:\\repo.\\x",
        "C:\\repo \\x",
    ],
)
def test_windows_mapping_rejects_ambiguous_paths(raw):
    with pytest.raises(ValueError, match="workspace_mapping_rejected"):
        windows_to_wsl(raw)


@pytest.mark.parametrize("raw", ["../a", "/etc/passwd", r"C:\a", r"a\..\b", "a:stream"])
def test_target_mapping_cannot_escape(temp_workspace, raw):
    mapping = WorkspaceMapping.create(temp_workspace)
    with pytest.raises(ValueError, match="workspace_mapping_rejected"):
        mapping.sandbox_path(raw)
    assert mapping.sandbox_path("目录/a.py") == "/workspace/目录/a.py"


def test_managed_default_is_external_and_workspace_specific(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("FIXLOOP_STATE_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME", str(tmp_path))
    first = tmp_path / "repo-a"
    second = tmp_path / "repo-b"
    first.mkdir()
    second.mkdir()
    one, two = managed_state_base(first), managed_state_base(second)
    assert one != two and not one.is_relative_to(first) and not two.is_relative_to(second)


def test_external_sessions_observations_and_collaboration(temp_workspace, spec):
    from agent_runtime.context_runtime import ObservationStore
    from agent_runtime.session_store import SessionStore
    from src.collaboration.repair_runtime import RepairCollaborationRuntime

    binding = SandboxTaskBinding.create(temp_workspace, temp_workspace, "task-1", spec)
    binding.save()
    SessionStore(str(temp_workspace), state_root=binding.state_root).save(
        {"id": "s", "history": []}
    )
    observations = ObservationStore({"id": "s"}, str(temp_workspace), binding.state_root)
    observations.put("read_file", {"path": "README.md"}, "observed")
    observations.close()
    RepairCollaborationRuntime(
        str(temp_workspace), "task-1", RepairState(issue_input="fix"), state_root=binding.state_root
    )
    assert (Path(binding.state_root) / ".agent" / "sessions" / "s.json").is_file()
    assert not (temp_workspace / ".agent").exists()


def test_binding_roundtrip_and_duplicate_id(temp_workspace, spec):
    binding = SandboxTaskBinding.create(temp_workspace, temp_workspace, "task-1", spec)
    binding.save()
    assert SandboxTaskBinding.load(temp_workspace, "task-1", spec) == binding
    with pytest.raises(FileExistsError):
        binding.save()
    for changed in (
        replace(spec, policy_version="s1-v2"),
        replace(spec, environment=replace(spec.environment, runtime_digest="c" * 64)),
    ):
        with pytest.raises(ValueError, match="identity_mismatch"):
            SandboxTaskBinding.load(temp_workspace, "task-1", changed)


def test_corrupt_binding_is_not_accepted(temp_workspace, spec):
    binding = SandboxTaskBinding.create(temp_workspace, temp_workspace, "task-1", spec)
    path = binding.save()
    raw = json.loads(path.read_text())
    raw["policy_version"] = "other"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="sandbox_task_unavailable"):
        SandboxTaskBinding.load(temp_workspace, "task-1", spec)


def test_binding_rejects_state_in_checkout(temp_workspace, spec):
    with pytest.raises(ValueError, match="state_root inside workspace"):
        SandboxTaskBinding.create(
            temp_workspace,
            temp_workspace,
            "task-1",
            replace(spec, state_base=str(temp_workspace / "state")),
        )


def test_workspace_directory_replaced_is_not_same_identity(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    mapping = WorkspaceMapping.create(root)
    root.rename(tmp_path / "old-repo")
    root.mkdir()
    with pytest.raises(ValueError, match="identity changed"):
        mapping.validate()


@pytest.mark.parametrize(
    "field,value",
    [("root", "/different"), ("state_root", "/different"), ("sandbox_run_id", "other")],
)
def test_dispatch_rejects_context_drift_before_backend(temp_workspace, spec, field, value):
    binding = SandboxTaskBinding.create(temp_workspace, temp_workspace, "task-1", spec)
    context = ToolContext(str(temp_workspace))
    binding.attach(context)
    calls = []
    context.sandbox_backend = SimpleNamespace(execute=lambda _: calls.append(True))
    setattr(context, field, value)
    with pytest.raises(ValueError, match="identity_mismatch"):
        execute_sandbox(context, "command", ("/toolchain/bin/python",), 1)
    assert calls == []


def test_worktree_binding_precedes_snapshot_and_trace(temp_workspace, spec, monkeypatch):
    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    roots = []
    orch = orchestrator(
        temp_workspace,
        spec,
        lambda binding: (
            roots.append(binding.mapping.host_root) or SimpleNamespace(task_binding=binding)
        ),
    )
    monkeypatch.setattr(orch, "_snapshot_repo", lambda: roots.append(orch._repo_root) or {})

    def impl(state, initial_snapshot):
        assert state.node_timings["sandbox_task_binding"]["workspace_ownership"] == "task"
        assert orch.patcher.state_root == orch.patcher.tool_context.state_root
        orch._maybe_enter_worktree(state, None)  # no second worktree or backend rebind
        orch._maybe_leave_worktree()
        return state

    monkeypatch.setattr(orch, "_repair_impl", impl)
    result = orch.repair("fix", run_id="task-1", repair_timeout_s=0)
    target = Path(roots[0])
    assert roots == [str(target), str(target)] and target != temp_workspace
    assert target.exists() and not (target / ".agent").exists()
    assert result.node_timings["sandbox_task_binding"]["mapping"]["host_root"] == str(target)
    assert not (temp_workspace / ".agent").exists()


def test_worktree_creation_failure_never_runs_original_repo(temp_workspace, spec, monkeypatch):
    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    (temp_workspace / "README.md").write_text("user edit")
    called = []
    orch = orchestrator(temp_workspace, spec, lambda binding: called.append(binding))
    monkeypatch.setattr(
        orch, "_snapshot_repo", lambda: pytest.fail("snapshot after failed creation")
    )
    with pytest.raises(WorktreeError, match="user changes"):
        orch.repair("fix", run_id="task-1", repair_timeout_s=0)
    assert called == [] and (temp_workspace / "README.md").read_text() == "user edit"


def test_resume_reuses_retained_worktree_and_checks_checkpoint(temp_workspace, spec, monkeypatch):
    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    first = orchestrator(temp_workspace, spec)
    state = RepairState(issue_input="fix", repair_run_id="task-1")
    first._prepare_sandbox_task(state, resume=False)
    root = Path(first._repo_root)
    (root / "README.md").write_text("retained result")
    retain_worktree(first._sandbox_worktree_handle)
    save_repair_checkpoint(state, str(root), state_root=first._sandbox_context.state_root)
    second = orchestrator(temp_workspace, spec)
    monkeypatch.setattr(second, "_snapshot_repo", lambda: {})
    monkeypatch.setattr(second, "_repair_impl", lambda state, initial_snapshot: state)
    restored = second.repair("fix", resume_run_id="task-1", repair_timeout_s=0)
    assert restored.status != "recovery_required"
    assert Path(second._repo_root) == root and (root / "README.md").read_text() == "retained result"
    # A valid checkpoint for this run with the wrong task binding must still fail closed.
    state.node_timings["sandbox_task_binding"]["policy_version"] = "other"
    save_repair_checkpoint(state, str(root), state_root=first._sandbox_context.state_root)
    third = orchestrator(temp_workspace, spec)
    monkeypatch.setattr(
        third, "_snapshot_repo", lambda: pytest.fail("snapshot after mismatched checkpoint")
    )
    rejected = third.repair("fix", resume_run_id="task-1", repair_timeout_s=0)
    assert rejected.status == "recovery_required"


def test_existing_workspace_preserves_dirty_files(temp_workspace, spec, monkeypatch):
    monkeypatch.delenv("FIXLOOP_USE_WORKTREE", raising=False)
    (temp_workspace / "README.md").write_text("user edit")
    orch = orchestrator(temp_workspace, spec)
    state = RepairState(issue_input="fix", repair_run_id="task-1")
    orch._prepare_sandbox_task(state, resume=False)
    assert orch._repo_root == str(temp_workspace)
    assert (temp_workspace / "README.md").read_text() == "user edit"
    assert state.node_timings["sandbox_task_binding"]["workspace_ownership"] == "user"


def test_existing_user_worktree_is_never_managed_deleted(temp_workspace, spec, monkeypatch):
    handle = create_worktree(temp_workspace, "user-worktree", state_base=spec.state_base)
    user = adopt_worktree(temp_workspace, handle.path, "task-1", state_base=spec.state_base)
    monkeypatch.delenv("FIXLOOP_USE_WORKTREE", raising=False)
    orch = orchestrator(handle.path, spec)
    state = RepairState(issue_input="fix", repair_run_id="task-1")
    orch._prepare_sandbox_task(state, resume=False)
    orch._repair_ctx = RepairRunContext(worktree_handle=user)
    orch._maybe_leave_worktree(cancelled=True)
    assert handle.path.exists() and user.ownership == "user"


def test_legacy_creation_failure_also_stops(temp_workspace, spec, monkeypatch):
    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    monkeypatch.setenv("FIXLOOP_STATE_ROOT", spec.state_base)
    (temp_workspace / "README.md").write_text("user edit")
    agent = SimpleNamespace(tool_context=ToolContext(str(temp_workspace)), _cwd=str(temp_workspace))
    orch = Orchestrator(agent)
    orch._repair_ctx = RepairRunContext()
    with pytest.raises(WorktreeError, match="user changes"):
        orch._maybe_enter_worktree(RepairState(issue_input="fix", repair_run_id="task-1"), None)


def test_backend_cannot_ignore_final_binding(temp_workspace, spec, monkeypatch):
    monkeypatch.delenv("FIXLOOP_USE_WORKTREE", raising=False)
    orch = orchestrator(temp_workspace, spec, lambda _: SimpleNamespace())
    with pytest.raises(ValueError, match="backend_binding_mismatch"):
        orch._prepare_sandbox_task(
            RepairState(issue_input="fix", repair_run_id="task-1"), resume=False
        )


def test_worktree_conflicts_and_dirty_cleanup_preserve_user_data(temp_workspace, spec):
    handle = create_worktree(temp_workspace, "task-1", state_base=spec.state_base)
    (handle.path / "README.md").write_text("valuable result")
    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree(temp_workspace, "task-1", state_base=spec.state_base)
    assert not remove_worktree(temp_workspace, handle.path, state_base=spec.state_base)
    retain_worktree(handle)
    assert (
        cleanup_stale_worktrees(temp_workspace, max_age_hours=-1, state_base=spec.state_base) == 0
    )
    assert (handle.path / "README.md").read_text() == "valuable result"
    assert not (temp_workspace / ".agent").exists()


def test_git_remove_failure_has_no_rmtree_fallback(temp_workspace, spec, monkeypatch):
    import agent_runtime.worktree as module

    handle = create_worktree(temp_workspace, "task-1", state_base=spec.state_base)
    real_git = module._git
    monkeypatch.setattr(
        module,
        "_git",
        lambda cwd, *args, **kwargs: (
            SimpleNamespace(returncode=1)
            if args[:2] == ("worktree", "remove")
            else real_git(cwd, *args, **kwargs)
        ),
    )
    assert not remove_worktree(temp_workspace, handle.path, state_base=spec.state_base)
    assert handle.path.is_dir()


@pytest.mark.parametrize("task_id", ["CON", "nul.json", "task.", "../task"])
def test_task_ids_cannot_alias_windows_state_files(temp_workspace, spec, task_id):
    with pytest.raises(ValueError, match="identifier"):
        SandboxTaskBinding.create(temp_workspace, temp_workspace, task_id, spec)


@pytest.mark.parametrize(
    "field,value",
    [("mapping", []), ("repository_root", []), ("environment", []), ("schema_version", True)],
)
def test_malformed_binding_has_explicit_error(temp_workspace, spec, field, value):
    import hashlib

    binding = SandboxTaskBinding.create(temp_workspace, temp_workspace, "task-1", spec)
    path = binding.save()
    raw = binding.to_wire()
    raw[field] = value
    raw["checksum"] = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="sandbox_task_(unavailable|invalid)"):
        SandboxTaskBinding.load(temp_workspace, "task-1", spec)


def test_existing_branch_and_directory_are_never_replaced(temp_workspace, spec):
    from agent_runtime.worktree import _git, default_worktree_path

    _git(temp_workspace, "branch", "fixloop/wt-branch-conflict")
    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree(temp_workspace, "branch-conflict", state_base=spec.state_base)
    assert (
        _git(
            temp_workspace, "show-ref", "--verify", "refs/heads/fixloop/wt-branch-conflict"
        ).returncode
        == 0
    )
    path = default_worktree_path(temp_workspace, "path-conflict", state_base=spec.state_base)
    path.mkdir(parents=True)
    (path / "valuable.txt").write_text("keep")
    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree(temp_workspace, "path-conflict", state_base=spec.state_base)
    assert (path / "valuable.txt").read_text() == "keep"


def test_replaced_worktree_cannot_be_removed(temp_workspace, spec):
    handle = create_worktree(temp_workspace, "task-1", state_base=spec.state_base)
    retained = handle.path.with_name("retained-original")
    handle.path.rename(retained)
    handle.path.mkdir()
    # Even a replacement presenting the same Git identity is not the owned directory.
    (handle.path / ".git").write_bytes((retained / ".git").read_bytes())
    (handle.path / "valuable.txt").write_text("keep")
    with pytest.raises(WorktreeError, match="identity changed"):
        remove_worktree(temp_workspace, handle.path, force=True, state_base=spec.state_base)
    assert retained.exists() and (handle.path / "valuable.txt").read_text() == "keep"


def test_git_administration_never_runs_hooks_filters_or_inherited_git_env(
    temp_workspace, spec, monkeypatch
):
    import os
    import shlex
    import sys

    from agent_runtime.worktree import _git

    sentinel = Path(spec.state_base).parent / "external-execution.txt"
    command = " ".join(
        shlex.quote(part)
        for part in (
            Path(sys.executable).as_posix(),
            "-c",
            f"from pathlib import Path; Path({sentinel.as_posix()!r}).write_text('unsafe')",
        )
    )
    # Make attributes tracked before configuring an executable filter driver.
    (temp_workspace / ".gitattributes").write_text("README.md filter=host_attack\n")
    _git(temp_workspace, "add", ".gitattributes")
    _git(
        temp_workspace,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "attributes",
    )
    for key in ("clean", "smudge", "process"):
        _git(temp_workspace, "config", f"filter.host_attack.{key}", command)
    _git(temp_workspace, "config", "filter.host_attack.required", "true")
    hooks = temp_workspace / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\n" + command + "\n", encoding="utf-8")
    if os.name != "nt":
        hook.chmod(0o755)
    monkeypatch.setenv("GIT_DIR", str(temp_workspace / "absent"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(sentinel))
    handle = create_worktree(temp_workspace, "task-1", state_base=spec.state_base)
    assert handle.path.is_dir() and not sentinel.exists()


def test_dispatch_rejects_backend_and_coordinator_binding_drift(temp_workspace, spec):
    binding = SandboxTaskBinding.create(temp_workspace, temp_workspace, "task-1", spec)
    context = ToolContext(str(temp_workspace))
    binding.attach(context)
    context.sandbox_backend = SimpleNamespace()
    with pytest.raises(ValueError, match="backend_binding_mismatch"):
        execute_sandbox(context, "command", ("/toolchain/bin/python",), 1)
    context.sandbox_backend = SimpleNamespace(task_binding=binding)
    context.run_coordinator = SimpleNamespace(
        assert_can_dispatch=lambda: None, lease=SimpleNamespace(run_id="other", task_id="task-1")
    )
    with pytest.raises(ValueError, match="identity_mismatch"):
        execute_sandbox(context, "command", ("/toolchain/bin/python",), 1)


def test_real_agent_fixed_workspace_and_external_runtime_state(temp_workspace, spec, monkeypatch):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.session_store import SessionStore
    from agent_runtime.workspace import WorkspaceContext

    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    context = ToolContext(str(temp_workspace))
    agent = Agent(
        AgentConfig(),
        FakeModelClient([]),
        WorkspaceContext.build(str(temp_workspace)),
        tool_context=context,
    )
    orch = Orchestrator(
        agent,
        sandbox_context=context,
        sandbox_task_spec=spec,
        sandbox_backend_factory=metadata_backend,
    )
    state = RepairState(issue_input="fix", repair_run_id="task-1")
    orch._prepare_sandbox_task(state, resume=False)
    root = Path(orch._repo_root)
    binding = context.sandbox_task_binding
    assert root != temp_workspace and agent.workspace.repo_root == str(root)
    assert agent.session["session_scope"]["workspace_id"] == binding.mapping.workspace_id
    assert str(temp_workspace) not in agent._prefix.workspace_text
    monkeypatch.chdir(Path(spec.state_base))
    agent._detect_workspace_switch()
    assert context.root == agent._cwd == str(root)
    agent.record({"role": "user", "content": "external history"})
    assert agent.read_history()[0]["content"] == "external history"
    SessionStore(str(root), state_root=context.state_root).save(agent.session)
    orch._repair_ctx = RepairRunContext(worktree_handle=orch._sandbox_worktree_handle)
    try:
        orch._begin_repair_trace(state)
        assert orch._plan_binding.session.workspace == str(root)
        assert orch._collaboration_runtime.store.path.is_relative_to(Path(context.state_root))
        assert orch._plan_binding.coordinator.store.workspace == str(root)
        save_repair_checkpoint(state, str(root), state_root=context.state_root)
    finally:
        if orch._plan_binding is not None:
            orch._plan_binding.close()
        orch._maybe_leave_worktree()
    assert (Path(context.state_root) / ".agent" / "history.jsonl").is_file()
    assert not (root / ".agent").exists() and not (temp_workspace / ".agent").exists()


def test_backend_startup_failure_retains_worktree(temp_workspace, spec, monkeypatch):
    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    orch = orchestrator(temp_workspace, spec, lambda _: SimpleNamespace())
    with pytest.raises(ValueError, match="backend_binding_mismatch"):
        orch.repair("fix", run_id="task-1", repair_timeout_s=0)
    handle = orch._sandbox_worktree_handle
    assert handle.path.exists() and orch._repo_root == str(temp_workspace)
    lease = Path(spec.state_base) / "worktree-leases" / "task-1.json"
    assert json.loads(lease.read_text())["status"] == "retained"
    assert json.loads(lease.read_text())["reason"] == "startup_failed"


def test_binding_requires_fresh_agent_and_cannot_be_retargeted(temp_workspace, spec):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    agent = Agent(AgentConfig(), FakeModelClient([]), WorkspaceContext.build(str(temp_workspace)))
    binding = SandboxTaskBinding.create(temp_workspace, temp_workspace, "task-1", spec)
    backend = metadata_backend(binding)
    agent.session["history"] = [{"role": "user", "content": "previous task"}]
    with pytest.raises(ValueError, match="fresh_agent"):
        agent.bind_sandbox_task(binding, backend)
    assert agent.tool_context.sandbox_task_binding is None
    agent.session["history"] = []
    agent.bind_sandbox_task(binding, backend)
    with pytest.raises(ValueError, match="already_bound"):
        agent.bind_sandbox_task(binding, backend)


def test_long_external_plan_blob_can_be_read_and_replaced(tmp_path):
    import os

    from agent_runtime.plan_runtime.journal import PlanStore

    # The directory fits MAX_PATH while the content-addressed filename exceeds it.
    root = tmp_path / ("state-" + "x" * max(1, 207 - len(str(tmp_path))))
    store = PlanStore(root, {"task_id": "task-1"})
    try:
        key = store.put_blob("external plan state")
        assert os.name != "nt" or len(str(root / "blobs" / key)) > 260
        assert store.get_blob(key) == "external plan state"
        assert store.put_blob("external plan state") == key
    finally:
        store.close()
