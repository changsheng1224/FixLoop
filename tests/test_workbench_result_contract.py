"""Repair adapter uses a typed result contract, independently of display text."""

from contextlib import closing
from types import SimpleNamespace

import pytest

from agent_runtime.cancellation import CancellationToken
from src.state import RepairState, RepairStatus, VerificationResult
from src.workbench.files import ProjectFiles
from src.workbench.project import ProjectExecutor


@pytest.mark.parametrize("status", list(RepairStatus))
@pytest.mark.parametrize("verified", [True, False])
def test_success_depends_on_typed_status_and_verification(tmp_path, monkeypatch, status, verified):
    monkeypatch.setattr(RepairStatus, "__str__", lambda self: "display:" + self.value)
    # The `fixed` contract needs a real passed run, not just all_passed: the
    # executor also requires execution_status "passed" and at least one test.
    verification = (
        VerificationResult(all_passed=True, execution_status="passed", total_tests=1)
        if verified
        else VerificationResult(all_passed=False, execution_status="failed", total_tests=1)
    )
    state = RepairState(
        issue_input="request",
        status=status,
        verification_result=verification,
    )

    class Orch:
        """Stands in for the repair service, with the surface the executor uses."""

        def __init__(self):
            self.session = SimpleNamespace(active_agents=[], progress=None)

        def run(self, request):
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
    expected = (
        "succeeded"
        if status is RepairStatus.FIXED and verified
        else "waiting_approval"
        if status is RepairStatus.WAITING_APPROVAL
        else "failed"
    )
    assert result.status == expected
    assert result.artifacts["repair_status"] == status.value
