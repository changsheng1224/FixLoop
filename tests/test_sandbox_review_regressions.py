"""Regression coverage for sandbox entry, owner takeover and delivery."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.cancellation import CancelledError
from agent_runtime.linux_sandbox.permissions import PermissionService
from agent_runtime.run_coordination.coordinator import RunCoordinator
from src.repair_cli import run_repair_cli
from src.repair_workspace import GitSnapshot, git_run
from src.state import VerificationResult
from tests.test_repair_cli_lifecycle import arguments, patch_state, redirect_clone, result
from tests.test_sandbox_permissions import installer


def test_remote_sandbox_preserves_backend_options(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    redirect_clone(monkeypatch, temp_workspace)
    output = tmp_path / "result"
    args = arguments(
        "owner/project", output, execution_backend="wsl_bwrap", code_exploration_mode="text"
    )

    def execute(repo, token, ready):
        assert args.execution_tier == "auto"
        assert args.require_sandbox is False
        ready(repo)
        (Path(repo) / "README.md").write_text("repaired\n")
        return patch_state(
            args.issue, verification_result=VerificationResult(all_passed=True, total_tests=1)
        )

    assert run_repair_cli(args, execute) == 0
    assert result(output)["patch_available"]


@pytest.mark.parametrize("ending", ["success", "cancel", "error"])
def test_cli_exports_bound_worktree_even_after_interruption(
    temp_workspace, tmp_path, monkeypatch, ending
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    output, worktree = tmp_path / "result", tmp_path / "task-worktree"
    original = (temp_workspace / "README.md").read_bytes()
    args = arguments(
        temp_workspace, output, execution_backend="wsl_bwrap", code_exploration_mode="text"
    )

    def execute(repo, token, ready):
        git_run(Path(repo), "worktree", "add", "--detach", str(worktree), "HEAD")
        ready(str(worktree))
        (worktree / "README.md").write_text("task repaired\n")
        (worktree / "new.py").write_text("value = 2\n")
        if ending == "cancel":
            raise CancelledError("user")
        if ending == "error":
            raise RuntimeError("interrupted after edit")
        return patch_state(
            args.issue, verification_result=VerificationResult(all_passed=True, total_tests=1)
        )

    assert run_repair_cli(args, execute) == {"success": 0, "cancel": 130, "error": 1}[ending]
    assert (temp_workspace / "README.md").read_bytes() == original
    assert result(output)["repo_path"] == str(worktree.resolve())
    assert set(result(output)["changed_files"]) == {"README.md", "new.py"}
    assert b"task repaired" in (output / "patch.diff").read_bytes()
    git_run(temp_workspace, "apply", "--check", str(output / "patch.diff"))


def test_sandbox_delivery_disables_repository_executables(temp_workspace, tmp_path):
    hook = tmp_path / "untrusted.sh"
    hook.write_text("#!/bin/sh\necho ran > host-executed.txt\nprintf '\\n'\n")
    hook.chmod(0o755)
    git_run(temp_workspace, "config", "core.fsmonitor", str(hook))
    git_run(temp_workspace, "config", "filter.untrusted.clean", f'sh "{hook.as_posix()}"')
    git_run(temp_workspace, "config", "filter.untrusted.required", "true")
    (temp_workspace / ".gitattributes").write_text("README.md filter=untrusted\n")
    output = tmp_path / "output"
    output.mkdir()
    snapshot = GitSnapshot(temp_workspace, output, sandbox=True)
    try:
        (temp_workspace / "README.md").write_text("safe replacement\n")
        patch, files = snapshot.export()
        assert "README.md" in files and b"safe replacement" in patch
        assert not (temp_workspace / "host-executed.txt").exists()
    finally:
        snapshot.close()


@pytest.mark.parametrize("decision", [True, False, None])
def test_approval_survives_durable_owner_takeover(tmp_path, decision):
    repo, state = tmp_path / "repo", tmp_path / "state"
    repo.mkdir()
    old = RunCoordinator(str(repo), "task", "run", state_root=str(state))
    old.acquire()
    old.reconcile()
    lease = old.lease
    identity = {"workspace": lease.workspace_id, "task": "task", "run": "run"}
    service = PermissionService(identity, state_root=state, workspace=repo)
    req = installer(
        workspace_id=lease.workspace_id,
        owner_token=lease.owner_token,
        generation=lease.generation,
        coordination_revision=lease.coordination_revision,
    )
    versions = {"deps/demo.whl": "a" * 64}
    pending = service.authorize(req, coordinator=old, input_versions=versions)
    if decision is not None:
        service.resolve(pending["approval_id"], allow=decision)
    old.finish("released")

    new = RunCoordinator(str(repo), "task", "run", state_root=str(state))
    new.acquire()
    new.reconcile()
    resumed = replace(
        req,
        call_id="resumed",
        owner_token=new.lease.owner_token,
        generation=new.lease.generation,
        coordination_revision=new.lease.coordination_revision,
    )
    service = PermissionService(identity, state_root=state, workspace=repo)
    answer = service.authorize(resumed, coordinator=new, input_versions=versions)
    if decision is None:
        assert answer["status"] == "approval_required"
        assert answer["approval_id"] == pending["approval_id"]
        service.resolve(pending["approval_id"], allow=True)
        answer = service.authorize(resumed, coordinator=new, input_versions=versions)
    if decision is False:
        assert answer["status"] == "approval_denied"
    else:
        assert answer["status"] == "allowed"
        evidence = service.consume(resumed, input_versions=versions)
        service.assert_current(evidence)
        assert service.snapshot()["pending"]["status"] == "consumed"
        with pytest.raises(ValueError, match="authorization_replayed"):
            service.authorize(resumed, coordinator=new, input_versions=versions)
    new.finish("released")


@pytest.mark.parametrize("change", ["arguments", "inputs", "task", "stale_owner"])
def test_takeover_does_not_expand_approved_invocation(tmp_path, change):
    from agent_runtime.run_coordination.store import CoordinationError

    repo = tmp_path / "repo"
    repo.mkdir()
    owner = RunCoordinator(str(repo), "task", "run", state_root=str(tmp_path / "state"))
    owner.acquire()
    owner.reconcile()
    req = installer(
        workspace_id=owner.lease.workspace_id,
        owner_token=owner.lease.owner_token,
        generation=owner.lease.generation,
        coordination_revision=owner.lease.coordination_revision,
    )
    service = PermissionService({})
    versions = {"deps/demo.whl": "a" * 64}
    pending = service.authorize(req, input_versions=versions)
    service.resolve(pending["approval_id"], allow=True)
    owner.finish("released")
    replacement = RunCoordinator(str(repo), "task", "run", state_root=str(tmp_path / "state"))
    replacement.acquire()
    replacement.reconcile()
    resumed = replace(
        req,
        call_id="next",
        owner_token=replacement.lease.owner_token,
        generation=replacement.lease.generation,
        coordination_revision=replacement.lease.coordination_revision,
    )
    if change == "arguments":
        resumed = replace(resumed, timeout_s=19)
    elif change == "inputs":
        versions = {"deps/demo.whl": "b" * 64}
    elif change == "task":
        resumed = replace(resumed, task_id="another")
    if change == "stale_owner":
        with pytest.raises(CoordinationError):
            service.authorize(resumed, coordinator=owner, input_versions=versions)
    else:
        assert (
            service.authorize(resumed, coordinator=replacement, input_versions=versions)["status"]
            == "approval_pending"
        )
    assert not service.snapshot()["tickets"]
    replacement.finish("released")
