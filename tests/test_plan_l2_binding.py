"""Production L2 path with actual disk edits, pytest and process-level recovery."""

import json
import os
import subprocess
import sys
import threading
import time

from tests.plan_l2_support import repair_fixture


def test_read_cancellation_propagates_while_tool_is_running():
    import pytest

    from agent_runtime.cancellation import CancellationToken, CancelledError
    from src.repair.plan_binding import _ReadCancellationToken

    parent = CancellationToken()
    first = _ReadCancellationToken(parent)
    second = _ReadCancellationToken(parent)
    entered = threading.Event()
    observed = []

    def running_read():
        entered.set()
        while not second.is_cancelled:
            time.sleep(0.005)
        observed.append(second.reason)

    worker = threading.Thread(target=running_read)
    worker.start()
    assert entered.wait(1)
    first.cancel("read_cancelled")
    assert first.is_cancelled
    assert not second.is_cancelled and not parent.is_cancelled
    parent.cancel("repair_timeout")
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert observed == ["repair_timeout"]
    with pytest.raises(CancelledError, match="repair_timeout"):
        second.check()
    assert second.cause == parent.cause
    assert first.reason == "read_cancelled"


def test_actual_l2_tools_and_pytest(tmp_path):
    orch, state, client = repair_fixture(tmp_path)
    try:
        patches, meta = orch._run_patcher_toolized(state, "Read value.py and fix answer()", {})
        assert patches, (meta, state.agent_errors, state.node_timings)
        assert orch._plan_binding.session.plan.node("edit").status == "succeeded"
        result = orch._run_verifier(state)
        assert result.all_passed, result.failure_logs
        assert result.total_tests == 1
        assert orch._plan_binding.session.plan.status == "completed"
        assert (tmp_path / "value.py").read_text() == "def answer():\n    return 2\n"
        assert "plan_checkpoint" in state.node_timings
    finally:
        if orch._plan_binding:
            orch._plan_binding.close()


def test_public_repair_entrypoint_and_resume_keep_run_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXLOOP_PROGRESS_HEARTBEAT", "0")
    orch, seed, client = repair_fixture(tmp_path, full=True)
    result = orch.repair(seed.issue_input, repair_timeout_s=0, resume_run_id=seed.repair_run_id)
    assert result.status == "fixed", (result.agent_errors, result.node_timings)
    assert result.repair_run_id == seed.repair_run_id
    assert result.node_timings["plan_progress"]["nodes"][-1]["status"] == "succeeded"
    assert orch._plan_binding is None
    resumed, _, _ = repair_fixture(tmp_path, resume=True, full=True)
    restored = resumed.repair(
        seed.issue_input, repair_timeout_s=0, resume_run_id=result.repair_run_id
    )
    assert restored.status == "fixed", restored.agent_errors
    assert restored.repair_run_id == result.repair_run_id


def test_l2_process_crash_adopts_patch_and_runs_verifier_once(tmp_path):
    code = """
import os, sys
from tests.plan_l2_support import repair_fixture
orch, state, client = repair_fixture(sys.argv[1])
orch._plan_fault = lambda point: os._exit(74) if point == 'tool_result_recorded' and 'return 2' in (__import__('pathlib').Path(sys.argv[1])/'value.py').read_text() else None
orch._run_patcher_toolized(state, 'Read value.py and fix answer()', {})
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        timeout=60,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 74, child.stderr
    orch, state, client = repair_fixture(tmp_path, resume=True)
    try:
        patches, meta = orch._run_patcher_toolized(state, "continue repair", {})
        assert patches, (meta, state.agent_errors)
        assert meta["edit_mode"] == "plan_resume_adopted"
        result = orch._run_verifier(state)
        assert result.all_passed
        store = orch._plan_binding.session.store
        writes = [
            v for v in store.latest("operation", "operation_id").values() if v["effect"] == "write"
        ]
        assert len(writes) == 1
        verifies = [
            v for v in store.latest("attempt", "attempt_id").values() if v["kind"] == "verify"
        ]
        assert len(verifies) == 1
        assert orch._plan_binding.report["adopted"] == ["edit"]
        manifest = {
            "actual_write_calls": len(writes),
            "actual_verify_calls": len(verifies),
            "recovery": orch._plan_binding.report,
            "plan": orch._plan_binding.session.plan.to_dict(),
        }
        (store.root / "acceptance.json").write_text(json.dumps(manifest, indent=2))
    finally:
        if orch._plan_binding:
            orch._plan_binding.close()


def test_failed_verifier_rollback_and_bounded_replan(tmp_path):
    orch, state, client = repair_fixture(tmp_path)
    (tmp_path / "test_value.py").write_text(
        "from value import answer\ndef test_answer():\n    assert answer() == 3\n"
    )
    state.issue_input = "Fix value.py: answer() should return 3."
    client._outputs.extend(
        [
            '{"conclusion":"The failing verifier expects 3. Use fresh repository evidence and change the return value."}',
            '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
            '<tool>{"name":"patch_file","args":{"path":"value.py","old_text":"return 1","new_text":"return 3"}}</tool>',
            "<final>Applied corrected patch.</final>",
        ]
    )
    before = orch._snapshot_repo()
    try:
        patches, meta = orch._run_patcher_toolized(state, "repair", {})
        assert patches, (meta, state.agent_errors)
        assert not orch._run_verifier(state).all_passed
        orch._restore_repo_snapshot(before)
        state.retry_count += 1
        patches, meta = orch._run_patcher_toolized(state, "use verifier evidence", {})
        assert patches, (meta, state.agent_errors)
        assert orch._plan_binding.session.plan.plan_version == 2
        assert orch._run_verifier(state).all_passed
        assert (
            len(
                [
                    e
                    for e in orch._plan_binding.session.store.events()
                    if e["kind"] == "trace" and e["payload"]["event"] == "replan_committed"
                ]
            )
            == 1
        )
    finally:
        if orch._plan_binding:
            orch._plan_binding.close()


def test_public_repair_resume_after_process_crash(tmp_path, monkeypatch):
    from agent_runtime.plan_runtime.session import PlanSession

    monkeypatch.setenv("FIXLOOP_PROGRESS_HEARTBEAT", "0")
    code = """
import os, sys
from pathlib import Path
from tests.plan_l2_support import repair_fixture
orch, seed, client = repair_fixture(sys.argv[1], full=True)
orch._plan_fault = lambda point: os._exit(76) if point == 'tool_result_recorded' and 'return 2' in (Path(sys.argv[1])/'value.py').read_text() else None
orch.repair(seed.issue_input, repair_timeout_s=0, resume_run_id=seed.repair_run_id)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        timeout=90,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 76, child.stderr
    orch, seed, client = repair_fixture(tmp_path, resume=True, full=True)
    result = orch.repair(seed.issue_input, repair_timeout_s=0, resume_run_id=seed.repair_run_id)
    assert result.status == "fixed", (result.agent_errors, result.node_timings)
    assert client.session_usage["calls"] == 0
    with PlanSession(
        str(tmp_path), result.repair_run_id, result.repair_run_id, orch.patcher.tools
    ) as session:
        operations = session.store.latest("operation", "operation_id")
        assert len([o for o in operations.values() if o["effect"] == "write"]) == 1
        assert session.plan.status == "completed"
        (session.store.root / "public_acceptance.json").write_text(
            json.dumps(
                {
                    "status": result.status,
                    "run_id": result.repair_run_id,
                    "resumed_model_calls": client.session_usage["calls"],
                    "actual_write_calls": 1,
                    "plan": session.plan.to_dict(),
                    "journal_sequence": session.store.events()[-1]["seq"],
                },
                indent=2,
            )
        )
