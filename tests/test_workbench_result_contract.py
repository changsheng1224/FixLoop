"""Repair adapter uses a typed result contract, independently of display text."""

from contextlib import closing

import pytest

from agent_runtime.cancellation import CancellationToken
from src.state import RepairState, RepairStatus, VerificationResult
from src.workbench.files import ProjectFiles
from src.workbench.project import ProjectExecutor


@pytest.mark.parametrize("status", list(RepairStatus))
@pytest.mark.parametrize("verified", [True, False])
def test_success_depends_on_typed_status_and_verification(tmp_path, monkeypatch, status, verified):
    monkeypatch.setattr(RepairStatus, "__str__", lambda self: "display:" + self.value)
    state = RepairState(
        issue_input="request",
        status=status,
        verification_result=VerificationResult(all_passed=verified),
    )

    class Orch:
        _active_agents = ()

        def repair(self, *args, **kwargs):
            return state

    repo = tmp_path / "repo"
    repo.mkdir()
    executor = ProjectExecutor(lambda: object(), repair_factory=lambda *a, **kw: Orch())
    with closing(ProjectFiles(repo)) as files:
        result = executor(
            mode="repair",
            files=files,
            messages=[{"role": "user", "content": "request"}],
            token=CancellationToken(),
            on_chunk=lambda _: None,
            publish=lambda *a: None,
            run_id="run",
            state_root=tmp_path / "state",
        )
    assert result.status == ("succeeded" if status is RepairStatus.FIXED and verified else "failed")
    assert result.artifacts["repair_status"] == status.value
