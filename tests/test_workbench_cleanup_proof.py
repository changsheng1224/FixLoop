"""An inaccessible resource record is uncertainty, not proof of resource absence."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.state_root import project_id, workspace_id
from src.workbench import process_scope
from src.workbench.supervisor import reconcile_sandbox


def resource(tmp_path):
    source, workspace = tmp_path / "project", tmp_path / "tree"
    return {
        "workspace": str(workspace),
        "workspace_id": workspace_id(workspace),
        "project_path": str(source),
        "project_id": project_id(source),
        "root_identity": [],
        "state_root": str(tmp_path / "runtime"),
        "run_id": "run",
    }


@pytest.mark.parametrize("kind", ["sandbox_binding", "cgroup"])
def test_inaccessible_resource_never_proves_cleanup(tmp_path, monkeypatch, kind):
    original_stat = Path.stat
    name = "sandbox-task.json" if kind == "sandbox_binding" else "unreadable-cgroup"

    def inaccessible(path, *args, **kwargs):
        if path.name == name:
            raise PermissionError("injected resource access denial")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible)
    monkeypatch.setattr(Path, "lstat", inaccessible)
    if kind == "sandbox_binding":
        result = reconcile_sandbox(resource(tmp_path))
    else:
        # Simulate the POSIX branch only; this does not claim a real Linux run.
        monkeypatch.setattr(process_scope, "os", SimpleNamespace(name="posix"))
        monkeypatch.setattr(process_scope, "confirmed_exited", lambda _: True)
        result = process_scope.reconcile_scope(
            {
                "backend": "linux_cgroup",
                "state": "running",
                "scope": str(tmp_path / name),
                "worker": {"pid": 1},
            }
        )
    assert result is False


def test_definitely_absent_binding_after_worker_exit_is_safe(tmp_path):
    assert reconcile_sandbox(resource(tmp_path))
