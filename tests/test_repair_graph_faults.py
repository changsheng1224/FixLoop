"""D5 fault boundaries through the public repair service and the real graph."""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.linux_sandbox.permissions import SandboxApprovalRequired
from agent_runtime.plan_runtime.session import PlanSession
from agent_runtime.run_coordination.store import RunCoordinationStore
from src.repair.checkpoint_load import load_repair_checkpoint
from src.repair.graph_stages import RepairStages
from src.repair.service import RepairRequest, RepairService
from tests.plan_l2_support import repair_fixture
from tests.test_repair_flow_parity import flow as flow


def test_stage_pause_without_kernel_receipts_requires_recovery(tmp_path, monkeypatch):
    """An arbitrary stage exception cannot certify planned resource cleanup."""
    session, seed, client = repair_fixture(tmp_path, full=True)

    def patch(self):
        raise SandboxApprovalRequired("stage-approval")

    monkeypatch.setattr(RepairStages, "patch", patch)
    paused = RepairService(session).run(
        RepairRequest(seed.issue_input, timeout_s=0, run_id=seed.repair_run_id)
    )
    assert paused.status == "recovery_required"
    saved = load_repair_checkpoint(str(tmp_path), seed.repair_run_id, require_valid=True)
    assert saved["control"]["pending_approval_id"] == "stage-approval"
    assert client.session_usage["calls"] == 0
    assert (tmp_path / "value.py").read_text() == "def answer():\n    return 1\n"
    assert session.plan_binding is None and not session.run_lock.locked()
    snapshot = RunCoordinationStore(str(tmp_path)).snapshot(seed.repair_run_id)
    assert snapshot.status == "recovery_required" and not snapshot.owner_token
    assert any(resource.cleanup != "confirmed" for resource in snapshot.resources)


def test_approval_reentry_uses_existing_checkpoint_and_public_entry(flow, monkeypatch):
    """L2 orchestration contract; native permission receipts are tested in L1."""
    session, run, _ = flow
    patcher = session.patcher_execution.run_patcher
    monkeypatch.setattr(
        session.patcher_execution,
        "run_patcher",
        Mock(side_effect=SandboxApprovalRequired("stage-approval")),
    )
    existing = load_repair_checkpoint(session.repo_root, "parity")
    paused = run(**({} if existing else {"run_id": "parity"}))
    assert paused.status == "waiting_approval"
    saved = load_repair_checkpoint(session.repo_root, paused.repair_run_id, require_valid=True)
    assert saved["control"]["pending_approval_id"] == "stage-approval"
    monkeypatch.setattr(session.patcher_execution, "run_patcher", patcher)
    result = RepairService(session).run(
        RepairRequest(paused.issue_input, timeout_s=0, resume_run_id=paused.repair_run_id)
    )
    assert result.status == "pending_verify", result.agent_errors
    assert result.repair_run_id == paused.repair_run_id
    assert session.request is session.repair_ctx is None and not session.run_lock.locked()


@pytest.mark.parametrize("cut", ["patch", "verify"])
def test_process_exit_between_graph_stages_adopts_kernel_facts(tmp_path, monkeypatch, cut):
    monkeypatch.setenv("FIXLOOP_PROGRESS_HEARTBEAT", "0")
    program = """
import os, sys
from src.repair.graph_stages import RepairStages
from src.repair.service import RepairRequest, RepairService
from tests.plan_l2_support import repair_fixture
stage = sys.argv[2]
original = getattr(RepairStages, stage)
def cut(self):
    original(self)
    os._exit(81)
setattr(RepairStages, stage, cut)
session, seed, _ = repair_fixture(sys.argv[1], full=True)
RepairService(session).run(RepairRequest(seed.issue_input, timeout_s=0, run_id=seed.repair_run_id))
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), cut],
        timeout=90,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 81, child.stderr
    assert (tmp_path / "value.py").read_text() == "def answer():\n    return 2\n"
    session, seed, client = repair_fixture(tmp_path, resume=True, full=True)
    verify = Mock(wraps=session.verification.run_verifier_impl)
    monkeypatch.setattr(session.verification, "run_verifier_impl", verify)
    result = RepairService(session).run(
        RepairRequest(seed.issue_input, timeout_s=0, resume_run_id=seed.repair_run_id)
    )
    assert result.status == "fixed", result.agent_errors
    assert client.session_usage["calls"] == 0
    assert verify.call_count == (1 if cut == "patch" else 0)
    with PlanSession(
        str(tmp_path), result.repair_run_id, result.repair_run_id, session.patcher.tools
    ) as plan:
        operations = plan.store.latest("operation", "operation_id").values()
        assert sum(op["effect"] == "write" for op in operations) == 1
        assert plan.plan.status == "completed"
    assert session.plan_binding is None and not session.run_lock.locked()


def test_cancellation_between_nodes_rolls_back_before_return(tmp_path, monkeypatch):
    session, seed, _ = repair_fixture(tmp_path, full=True)
    token = CancellationToken()
    critic = RepairStages.critic
    verify = Mock(wraps=session.verification.run_verifier)

    def cancel_after_critic(self):
        route = critic(self)
        assert (tmp_path / "value.py").read_text() == "def answer():\n    return 2\n"
        token.cancel("user")
        return route

    monkeypatch.setattr(RepairStages, "critic", cancel_after_critic)
    monkeypatch.setattr(session.verification, "run_verifier", verify)
    result = RepairService(session).run(
        RepairRequest(seed.issue_input, timeout_s=0, run_id=seed.repair_run_id, cancel_token=token)
    )
    assert result.status == "user_cancel", result.agent_errors
    verify.assert_not_called()
    assert (tmp_path / "value.py").read_text() == "def answer():\n    return 1\n"
    snapshot = RunCoordinationStore(str(tmp_path)).snapshot(result.repair_run_id)
    assert snapshot.status == "cancelled" and not snapshot.owner_token
    assert all(resource.cleanup == "confirmed" for resource in snapshot.resources)
    assert not session.run_lock.locked()


@pytest.mark.parametrize("failure", ["scheduler", "worker_start"])
def test_graph_failures_preserve_public_cleanup(flow, monkeypatch, failure):
    session, run, _ = flow
    if failure == "scheduler":
        graph = SimpleNamespace(invoke=Mock(side_effect=RuntimeError("graph failure")))
        monkeypatch.setattr("src.repair.graph.repair_graph", lambda: graph)
    else:
        monkeypatch.setattr(
            "src.repair.graph_owner.Thread.start", Mock(side_effect=RuntimeError("graph failure"))
        )
    result = run()
    assert result.status == "failed"
    assert result.agent_errors["runtime"] == "graph failure"
    assert session.edit_lock is session.repair_ctx is session.request is None
    assert session.progress._hb_thread is None and not session.run_lock.locked()


def test_total_timeout_retains_live_graph_owner_and_budget(tmp_path, monkeypatch):
    session, seed, _ = repair_fixture(tmp_path, full=True)
    entered, release = threading.Event(), threading.Event()
    token = CancellationToken()
    timers = []

    class ControlledTimer:
        def __init__(self, seconds, callback):
            self.callback = callback
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    def blocked_patch(self):
        entered.set()
        assert release.wait(10)
        token.check()
        raise AssertionError("cancelled patch continued")

    monkeypatch.setattr("src.repair.execution.lifecycle.threading.Timer", ControlledTimer)
    monkeypatch.setattr(RepairStages, "patch", blocked_patch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            RepairService(session).run,
            RepairRequest(
                seed.issue_input,
                run_id=seed.repair_run_id,
                timeout_s=1,
                cancel_token=token,
            ),
        )
        try:
            assert entered.wait(10)
            timers[0].callback()
            assert token.is_cancelled and not future.done()
            assert session.run_lock.locked() and session.model_budget is not None
            assert session.patcher.budget_manager is session.model_budget
        finally:
            release.set()
        result = future.result(timeout=15)
    assert result.status == "timeout", result.agent_errors
    assert session.model_budget is session.model_deadline is session.repair_ctx is None
    assert session.patcher.cancel_token is session.patcher.budget_manager is None
    assert not session.run_lock.locked()
