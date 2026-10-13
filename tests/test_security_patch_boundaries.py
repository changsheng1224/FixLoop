from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from agent_runtime.security import check_shell_command, parse_shell_argv
from agent_runtime.sensitive_paths import is_sensitive_path
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_apply_patch
from agent_runtime.worktree import WorktreeError, worktree_base
from src.repair.execution.edit_lock import EditLockState


def test_shell_parser_rejects_composition_but_allows_python_probe():
    assert check_shell_command("echo ok && whoami")[0] is False
    assert parse_shell_argv('python -c "import time; time.sleep(0)"')[-1].startswith("import time")


def test_sensitive_control_plane_paths_are_blocked():
    for path in (
        ".git/config",
        ".github/workflows/ci.yml",
        ".npmrc",
        "infra/prod.tfvars",
        "docker-compose.prod.yml",
        "kubeconfig",
    ):
        assert is_sensitive_path(path), path


def test_patch_transaction_rolls_back_all_files(tmp_path):
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    (tmp_path / "b.py").write_bytes(b"x = 2\n")
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n@@ -1 +1 @@\n"
        "-x = 1\n+x = 3\n*** Update File: b.py\n@@ -1 +1 @@\n"
        "-missing\n+x = 4\n*** End Patch\n"
    )
    assert not tool_apply_patch(ToolContext(str(tmp_path)), {"patch": patch}).ok
    assert (tmp_path / "a.py").read_bytes() == b"x = 1\n"
    assert (tmp_path / "b.py").read_bytes() == b"x = 2\n"


def test_patch_allowlist_rejects_unapproved_path(tmp_path):
    target = tmp_path / "a.py"
    target.write_bytes(b"x = 1\n")
    context = ToolContext(str(tmp_path))
    context.edit_lock = EditLockState(repo_root=tmp_path, allowed_edit={"b.py"})
    context.edit_lock.write_serial = False
    patch = "*** Begin Patch\n*** Update File: a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n*** End Patch\n"
    assert not tool_apply_patch(context, {"patch": patch}).ok
    assert target.read_bytes() == b"x = 1\n"


def test_worktree_path_jail_rejects_escape(tmp_path):
    from agent_runtime.worktree import create_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external-state"
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with pytest.raises(WorktreeError):
        create_worktree(repo, "escape", path=tmp_path / "outside", state_base=external)
    assert worktree_base(repo, external).is_dir()


def test_binary_snapshot_roundtrip(temp_workspace):
    target = temp_workspace / "blob.bin"
    target.write_bytes(b"\x00\xffbefore")
    target.chmod(0o640)
    from agent_runtime.tool_context import ToolContext
    from agent_runtime.tool_executor import ToolExecutor

    fake_agent = SimpleNamespace(
        config=SimpleNamespace(approval="auto"),
        tools={},
        _tool_names=set(),
        session={},
        tool_context=ToolContext(str(temp_workspace)),
    )
    executor = ToolExecutor(agent=fake_agent, approval_policy="auto")
    snapshot = executor._capture_restore_snapshot()
    target.write_bytes(b"after")
    target.chmod(0o600)
    executor._restore_restore_snapshot(snapshot)
    assert target.read_bytes() == b"\x00\xffbefore"
    if os.name != "nt":
        assert os.stat(target).st_mode & 0o777 == 0o640


def test_security_trace_events_render_low_cardinality_metrics():
    from agent_runtime.metrics import _reset_registry_for_tests, get_registry
    from agent_runtime.observability.prom_from_trace import record_canonical_event

    _reset_registry_for_tests()
    record_canonical_event(
        {"event": "stale_patch_rejected", "status": "error", "payload": {"reason": "cas"}}
    )
    rendered = get_registry().render()
    assert "fixloop_stale_patch_rejections_total" in rendered
    assert "run_id" not in rendered
