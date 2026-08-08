from __future__ import annotations

import hashlib
import os
import subprocess
from types import SimpleNamespace

import pytest

from agent_runtime.security import check_shell_command, parse_shell_argv
from agent_runtime.sensitive_paths import is_sensitive_path
from agent_runtime.worktree import WorktreeError, worktree_base
from src.repair.execution.patch_applier import PatchApplier
from src.state import CandidatePatch


def test_shell_parser_rejects_composition_but_allows_python_probe():
    assert check_shell_command("echo ok && whoami")[0] is False
    assert parse_shell_argv('python -c "import time; time.sleep(0)"')[-1].startswith(
        "import time"
    )


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


def test_layer2_patch_transaction_rolls_back_all_files(tmp_path):
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("keep\n", encoding="utf-8")
    patches = [
        CandidatePatch(file_path="a.py", original_lines="old", patched_lines="new"),
        CandidatePatch(file_path="b.py", original_lines="missing", patched_lines="bad"),
    ]
    assert PatchApplier(str(tmp_path)).apply_patches(patches) == []
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "old\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "keep\n"


def test_layer2_patch_rejects_stale_base_hash(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    patch = CandidatePatch(
        file_path="a.py",
        original_lines="old",
        patched_lines="new",
        base_sha256=hashlib.sha256(b"different\n").hexdigest(),
    )
    applier = PatchApplier(str(tmp_path))
    assert applier.apply_patches([patch]) == []
    assert "stale_patch" in applier.last_apply_errors[0]


def test_patch_allowlist_rejects_unapproved_path(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    patch = CandidatePatch(file_path="a.py", original_lines="old", patched_lines="new")
    assert PatchApplier(str(tmp_path)).apply_patches(patch and [patch], allowed_paths={"b.py"}) == []
    assert target.read_text(encoding="utf-8") == "old\n"


def test_worktree_path_jail_rejects_escape(tmp_path):
    from agent_runtime.worktree import create_worktree

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    with pytest.raises(WorktreeError):
        create_worktree(tmp_path, "escape", path=tmp_path / ".." / "outside")
    assert worktree_base(tmp_path).is_dir()


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
