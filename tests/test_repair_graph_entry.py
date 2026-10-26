"""Public repair entry uses the graph while preserving pauses and cleanup."""

import threading
from concurrent.futures import ThreadPoolExecutor

from agent_runtime.linux_sandbox.permissions import SandboxApprovalRequired
from src.repair.graph import STAGE_ROUTES
from src.repair.graph_stages import RepairStages
from src.repair.service import RepairRequest, RepairService
from tests.plan_l2_support import repair_fixture
from tests.test_repair_flow_parity import flow as flow


def test_public_entry_executes_graph_stages_on_original_owner(flow, monkeypatch):
    session, run, events = flow
    owner_thread = threading.get_ident()
    stages = []
    for name in STAGE_ROUTES:
        method = getattr(RepairStages, name)

        def observe(self, original=method, stage=name):
            assert threading.get_ident() == owner_thread
            assert self.state is session.current_state
            stages.append(stage)
            return original(self)

        monkeypatch.setattr(RepairStages, name, observe)
    result = run()
    assert result.status == "pending_verify"
    assert stages[0] == "entry"
    assert stages[-4:] == ["critic", "protected_interface", "verification_gate", "pending_verify"]
    assert stages.count("patch") == 1
    names = [event.event for event in events]
    assert names.count("repair_started") == names.count("repair_finished") == 1
    assert session.request is None and session.repair_ctx is None


def test_graph_approval_uses_existing_pause_and_cleanup(flow, monkeypatch):
    session, run, events = flow

    def wait(state):
        raise SandboxApprovalRequired("graph-approval")

    monkeypatch.setattr(session.patcher_execution, "run_patcher", wait)
    result = run()
    assert result.status == "waiting_approval"
    assert result.control.pending_approval_id == "graph-approval"
    assert session.edit_lock is None and session.progress._hb_thread is None
    assert session.request is None and session.repair_ctx is None
    assert "verify_progress" not in [event.event for event in events]


def test_shared_compiled_graph_keeps_concurrent_sessions_isolated(tmp_path, monkeypatch):
    sessions = [repair_fixture(tmp_path / name, full=True)[:2] for name in ("first", "second")]
    ready = threading.Barrier(2)
    original = RepairStages.patch
    owners = {}

    def patch(self):
        owners[self.session.repo_root] = threading.get_ident()
        ready.wait(timeout=10)
        return original(self)

    monkeypatch.setattr(RepairStages, "patch", patch)

    def run(pair):
        session, seed = pair
        result = RepairService(session).run(
            RepairRequest(seed.issue_input, timeout_s=0, run_id=seed.repair_run_id)
        )
        assert owners[session.repo_root] == threading.get_ident()
        assert result is session.current_state
        assert result.status == "fixed", result.agent_errors
        assert session.request is None and session.plan_binding is None
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, sessions))
    assert results[0] is not results[1]
    assert len(set(owners.values())) == 2
    for name in ("first", "second"):
        assert (tmp_path / name / "value.py").read_text() == "def answer():\n    return 2\n"
