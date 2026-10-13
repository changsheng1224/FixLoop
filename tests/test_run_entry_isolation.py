"""Entry policy, actual checkout, independent concurrent runs and strict recovery."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.tool_context import ToolContext
from agent_runtime.worktree import WorktreeError
from src.orchestrator import Orchestrator
from src.repair.checkpoint_load import save_repair_checkpoint
from src.repair_factory import make_orchestrator_factory, wire_orchestrator
from src.state import RepairState


def host_orchestrator(repo, storage, *, policy="worktree", profile="legacy:host"):
    agent = SimpleNamespace(_cwd=str(repo), tool_context=ToolContext(str(repo)))
    return Orchestrator(
        agent, workspace_policy=policy, workspace_state_base=str(storage),
        execution_profile=profile,
    )


def test_concurrent_worktrees_use_final_roots_before_snapshot(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.delenv("FIXLOOP_USE_WORKTREE", raising=False)
    storage = tmp_path / "state"
    barrier = threading.Barrier(2)
    original = (temp_workspace / "README.md").read_bytes()
    runs = [host_orchestrator(temp_workspace, storage) for _ in range(2)]

    for index, orch in enumerate(runs):
        def snapshot(orch=orch):
            assert Path(orch._repo_root) != temp_workspace
            assert orch.patcher.tool_context.root == orch._repo_root
            return {}

        def impl(state, initial_snapshot, orch=orch, index=index):
            barrier.wait(timeout=20)
            root = Path(orch._repo_root)
            (root / "README.md").write_text(str(index))
            save_repair_checkpoint(state, str(root), state_root=orch.patcher.state_root)
            return state

        monkeypatch.setattr(orch, "_snapshot_repo", snapshot)
        monkeypatch.setattr(orch, "_repair_impl", impl)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(orch.repair, "fix", run_id=f"run-{i}", repair_timeout_s=0)
            for i, orch in enumerate(runs)
        ]
        results = [future.result(timeout=30) for future in futures]
    assert runs[0]._repo_root != runs[1]._repo_root
    assert runs[0].patcher.state_root != runs[1].patcher.state_root
    for i, (orch, state) in enumerate(zip(runs, results, strict=True)):
        assert (Path(orch._repo_root) / "README.md").read_text() == str(i)
        assert state.node_timings["run_entry"]["workspace"]["root"] == orch._repo_root
    assert (temp_workspace / "README.md").read_bytes() == original
    assert not (temp_workspace / ".agent").exists()


def test_provided_ignores_environment_flag_and_preserves_dirty_input(
    temp_workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    target = temp_workspace / "README.md"
    target.write_text("dirty input")
    orch = host_orchestrator(temp_workspace, tmp_path / "state", policy="provided")
    monkeypatch.setattr(orch, "_snapshot_repo", lambda: {})
    monkeypatch.setattr(orch, "_repair_impl", lambda state, initial_snapshot: state)
    state = orch.repair("fix", repair_timeout_s=0)
    assert orch._repo_root == str(temp_workspace)
    assert state.node_timings["run_entry"]["workspace_policy"] == "provided"
    assert target.read_text() == "dirty input"
    assert orch._sandbox_worktree_handle is None


def test_dirty_worktree_request_stops_before_callback_or_execution(
    temp_workspace, tmp_path, monkeypatch
):
    (temp_workspace / "README.md").write_text("user changes")
    orch = host_orchestrator(temp_workspace, tmp_path / "state")
    monkeypatch.setattr(orch, "_snapshot_repo", lambda: pytest.fail("snapshot ran"))
    with pytest.raises(WorktreeError, match="user changes"):
        orch.repair("fix", workspace_ready=lambda _: pytest.fail("workspace callback ran"))
    assert orch._repo_root == str(temp_workspace)


@pytest.mark.parametrize("damage", ["none", "profile", "missing", "directory"])
def test_host_recovery_uses_retained_checkout_or_rejects_entry_drift(
    temp_workspace, tmp_path, monkeypatch, damage
):
    storage = tmp_path / "state"
    first = host_orchestrator(temp_workspace, storage)
    state = RepairState(issue_input="fix", repair_run_id="retained")
    first._prepare_host_worktree(state, resume=False)
    root = Path(first._repo_root)
    (root / "README.md").write_text("candidate")
    state.node_timings["run_entry"] = first._workspace_entry()
    if damage == "missing":
        state.node_timings.pop("run_entry")
    if damage == "directory":
        state.node_timings["run_entry"]["project"]["identity"] = [0, 0]
    save_repair_checkpoint(state, str(root), state_root=first.patcher.state_root)
    second = host_orchestrator(
        temp_workspace, storage, profile="legacy:static" if damage == "profile" else "legacy:host"
    )
    monkeypatch.setattr(second, "_snapshot_repo", lambda: {})
    monkeypatch.setattr(
        second, "_repair_impl",
        lambda state, initial_snapshot: state if damage == "none" else pytest.fail("executed"),
    )
    result = second.repair("fix", resume_run_id="retained", repair_timeout_s=0)
    assert Path(second._repo_root) == root
    assert (root / "README.md").read_text() == "candidate"
    if damage == "none":
        assert result.status != "recovery_required"
    else:
        assert result.status == "recovery_required"
        assert result.agent_errors["checkpoint"] == "resume_workspace_policy_mismatch"


def test_real_host_agent_rebuilds_workspace_prefix_and_pins_cwd(temp_workspace, tmp_path, monkeypatch):
    orch = wire_orchestrator(
        FakeModelClient([]), str(temp_workspace), skip_verify=True,
        state_root=str(tmp_path / "state"), workspace_policy="worktree",
    )
    state = RepairState(issue_input="fix", repair_run_id="real-agent")
    orch._prepare_host_worktree(state, resume=False)
    agent = orch.patcher
    root = Path(orch._repo_root)
    assert agent.workspace.repo_root == agent.tool_context.root == str(root)
    assert agent.session["session_identity"]["run_id"] == state.repair_run_id
    assert str(temp_workspace) not in agent._prefix.workspace_text
    monkeypatch.chdir(tmp_path)
    agent._detect_workspace_switch()
    assert agent._cwd == str(root)
    assert agent.execute_tool("read_file", {"path": "README.md"}).ok
    assert not (root / ".agent").exists()


def test_real_host_pipeline_cli_delivery_uses_candidate_checkout(
    temp_workspace, tmp_path, monkeypatch
):
    from src.repair_cli import run_repair_cli
    from src.repair_workspace import git_run
    from tests.test_repair_cli_lifecycle import arguments, result

    (temp_workspace / "value.py").write_text("value = 1\n")
    (temp_workspace / "test_value.py").write_text(
        "from value import value\ndef test_value():\n    assert value == 2\n"
    )
    git_run(temp_workspace, "add", "value.py", "test_value.py")
    git_run(temp_workspace, "commit", "-m", "Python fixture")
    monkeypatch.setenv("FIXLOOP_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    client = FakeModelClient([
        '{"conclusion":"Change value.py to initialize value to 2."}',
        '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
        '<tool>{"name":"patch_file","args":'
        '{"path":"value.py","old_text":"value = 1","new_text":"value = 2"}}</tool>',
        "<final>Changed the initial value.</final>",
    ])
    output = tmp_path / "delivery"
    args = arguments(temp_workspace, output, workspace_policy="worktree", execution_tier="host")
    orch = wire_orchestrator(
        client, str(temp_workspace), execution_tier="host",
        runtime_overrides={"provider": "fake", "model": "fake-test", "approval": "auto",
                           "auto_context_extraction": False},
    )

    def execute(repo, token, workspace_ready):
        return orch.repair(
            args.issue, cancel_token=token, workspace_ready=workspace_ready, repair_timeout_s=0,
        )

    assert run_repair_cli(args, execute) == 0
    data = result(output)
    assert data["repo_path"] == orch._repo_root
    assert data["source_repo_path"] == str(temp_workspace)
    assert data["workspace_policy"] == "worktree" and data["execution_backend"] == "legacy"
    assert (temp_workspace / "value.py").read_text() == "value = 1\n"
    assert b"+value = 2" in (output / "patch.diff").read_bytes()
    assert data["run_entry"]["workspace"]["root"] == orch._repo_root
    assert orch._plan_binding is None  # The owner was released after completion.
    assert not (temp_workspace / ".agent").exists()


def test_factory_forwards_policy_and_trusted_source(monkeypatch):
    calls = []
    monkeypatch.setattr("src.repair_factory.wire_orchestrator", lambda *a, **kw: calls.append(kw))
    factory = make_orchestrator_factory(
        workspace_policy="provided", execution_backend="wsl_bwrap",
        state_root="storage", project_root="source",
    )
    factory("copy")
    assert calls[0]["workspace_policy"] == "provided"
    assert calls[0]["execution_backend"] == "wsl_bwrap"
    assert calls[0]["project_root"] == "source" and calls[0]["state_root"] == "storage"


def test_provided_real_agent_never_switches_to_process_cwd(temp_workspace, tmp_path, monkeypatch):
    orch = wire_orchestrator(
        FakeModelClient([]), str(temp_workspace), skip_verify=True, workspace_policy="provided",
    )
    agent = orch.patcher
    agent._detect_workspace_switch()
    (temp_workspace / "README.md").write_text("edited candidate")
    monkeypatch.chdir(tmp_path)
    agent._detect_workspace_switch()
    assert agent._cwd == agent.tool_context.root == str(temp_workspace)
    assert "edited candidate" in agent.execute_tool("read_file", {"path": "README.md"}).content


@pytest.mark.parametrize("kwargs", [
    {"workspace_policy": "auto"}, {"execution_tier": "unknown"},
    {"execution_backend": "unknown"},
])
def test_invalid_entry_decisions_fail_before_model_or_environment(tmp_path, monkeypatch, kwargs):
    monkeypatch.setattr("src.repair_factory.create_model_client", lambda *a, **kw: pytest.fail("model"))
    with pytest.raises(ValueError, match="unknown"):
        wire_orchestrator(None, str(tmp_path), **kwargs)


def test_workspace_policy_participates_in_sandbox_digest():
    from agent_runtime.linux_sandbox.task_context import SandboxEnvironmentIdentity
    from src.repair_factory import _sandbox_policy_digest

    environment = SandboxEnvironmentIdentity("Test", "1", "1", "a" * 64)
    assert _sandbox_policy_digest(environment, "provided") != _sandbox_policy_digest(
        environment, "worktree"
    )


@pytest.mark.parametrize("policy", [None, "provided"])
def test_cli_defaults_and_explicit_workspace_policy(monkeypatch, policy):
    import sys

    from src.cli import main

    calls = []
    monkeypatch.setattr("src.cli._repair", lambda args: calls.append(args) or 0)
    argv = ["fixloop", "repair", "--issue", "fix", "--execution-backend", "wsl_bwrap"]
    if policy:
        argv.extend(["--workspace-policy", policy])
    monkeypatch.setattr(sys, "argv", argv)
    assert main() == 0
    assert calls[0].workspace_policy == (policy or "worktree")
    assert calls[0].execution_backend == "wsl_bwrap"


def test_sandbox_resume_rejects_changed_workspace_policy(temp_workspace, tmp_path):
    from agent_runtime.linux_sandbox.task_context import SandboxEnvironmentIdentity, SandboxTaskSpec

    spec = SandboxTaskSpec(
        SandboxEnvironmentIdentity("Test", "1", "1", "a" * 64), "v1", "b" * 64,
        str(tmp_path / "state"),
    )

    def make(policy):
        context = ToolContext(str(temp_workspace))
        return Orchestrator(
            SimpleNamespace(_cwd=str(temp_workspace), tool_context=context),
            sandbox_context=context, sandbox_task_spec=spec, workspace_policy=policy,
            sandbox_backend_factory=lambda binding: SimpleNamespace(task_binding=binding),
        )

    first = make("worktree")
    state = RepairState(issue_input="fix", repair_run_id="sandbox")
    first._prepare_sandbox_task(state, resume=False)
    with pytest.raises(ValueError, match="resume_workspace_policy_mismatch"):
        make("provided")._prepare_sandbox_task(state, resume=True)
    record = Path(first.patcher.tool_context.state_root) / "sandbox-task.json"
    assert json.loads(record.read_text())["workspace_ownership"] == "task"
