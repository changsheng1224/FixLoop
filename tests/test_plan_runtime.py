"""Plan graph, permissions, scheduling, evidence, replan and corruption contracts."""

import json
import threading
from dataclasses import replace

import pytest

from agent_runtime.plan_runtime import Completion
from agent_runtime.plan_runtime.recovery import recover
from agent_runtime.plan_runtime.reducer import transition
from agent_runtime.plan_runtime.replan import replan
from agent_runtime.plan_runtime.scheduler import PlanScheduler, ReadBudget
from agent_runtime.plan_runtime.validate import validate_plan
from tests.plan_support import REGISTRY, read, session_for, simple_plan, through_analysis, write


@pytest.fixture
def session(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        yield session


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda p: replace(p, nodes=p.nodes + (p.nodes[0],)), "duplicate"),
        (
            lambda p: replace(
                p, nodes=(replace(p.nodes[0], depends_on=("missing",)), *p.nodes[1:])
            ),
            "unknown_dependency",
        ),
        (
            lambda p: replace(p, nodes=(replace(p.nodes[0], depends_on=("edit",)), *p.nodes[1:])),
            "plan_cycle",
        ),
        (
            lambda p: replace(
                p, nodes=(replace(p.nodes[0], completion=(Completion("looks_good"),)), *p.nodes[1:])
            ),
            "completion_type",
        ),
        (
            lambda p: replace(
                p,
                nodes=(
                    replace(p.nodes[0], tool_allowlist=("run_shell", "read_file")),
                    *p.nodes[1:],
                ),
            ),
            "readonly_tool",
        ),
        (
            lambda p: replace(
                p, nodes=tuple(replace(p.nodes[0], node_id=str(i)) for i in range(9))
            ),
            "node_budget",
        ),
        (lambda p: replace(p, task_id="wrong"), "identity_mismatch"),
    ],
)
def test_graph_rejections(session, mutate, reason):
    with pytest.raises(ValueError, match=reason):
        validate_plan(mutate(session.plan).seal(), REGISTRY, identity=session.identity)


def test_success_without_completion_blocks_downstream(session):
    session.run_node("read-0", lambda a: {"status": "success"})
    assert session.plan.node("read-0").status == "failed"
    assert session.plan.node("analysis").status == "blocked"
    assert session.plan.node("verify").status == "blocked"


def test_actual_write_and_tests_are_required(session):
    through_analysis(session)
    version = session.plan.plan_version
    session.run_node("edit", lambda a: write(session, a))
    assert session.plan.node("edit").status == "succeeded"
    assert session.plan.plan_version == version
    assert session.plan.state_revision > 0
    session.run_node(
        "verify",
        lambda a: session.verify_result(
            a, {"all_passed": True, "total_tests": 0}, {"command": ["pytest"], "completed": True}
        ),
    )
    assert session.plan.node("verify").status == "failed"


def test_two_reads_have_atomic_budget_and_owner_reducer(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=3))
        barrier = threading.Barrier(2)
        owner = threading.get_ident()
        workers = []

        def callback(a):
            workers.append(threading.get_ident())
            barrier.wait(timeout=5)
            return read(session, a)

        budget = ReadBudget(2)
        scheduler = PlanScheduler(session, budget)
        assert len(scheduler.run_reads({f"read-{i}": callback for i in range(3)})) == 2
        assert len(set(workers)) == 2 and owner not in workers
        assert budget.used == 2
        with pytest.raises(ValueError, match="read_budget"):
            scheduler.run_reads({"read-2": callback})
        assert session.plan.node("analysis").status == "pending"


def test_external_file_change_invalidates_evidence(session):
    session.run_node("read-0", lambda a: read(session, a))
    ref = session.plan.node("read-0").output_evidence_refs[0]
    assert session.evidence.valid(ref)
    from pathlib import Path

    (Path(session.workspace) / "value.py").write_text("external\n")
    assert not session.evidence.valid(ref)
    report = recover(session)
    assert "read-0" in report["stale"]
    assert session.plan.node("edit").status == "blocked"


def test_replan_preserves_history_is_bounded_and_atomic(session):
    session.run_node("read-0", lambda a: read(session, a))
    refs = list(session.plan.node("read-0").output_evidence_refs)
    initial = session.plan
    illegal = replace(
        initial, nodes=(replace(initial.nodes[0], objective="new semantic"), *initial.nodes[1:])
    ).seal()
    with pytest.raises(ValueError, match="semantics_changed"):
        replan(session, illegal, reason="new evidence", evidence_refs=refs)
    assert session.plan.plan_checksum == initial.plan_checksum
    for _ in range(2):
        replan(session, session.plan, reason="new evidence", evidence_refs=refs)
    assert session.plan.plan_version == 3
    assert session.plan.parent_plan_checksum
    with pytest.raises(ValueError, match="replan_budget"):
        replan(session, session.plan, reason="new evidence", evidence_refs=refs)
    assert len([e for e in session.store.events() if e["kind"] == "plan"]) > 3


def test_late_results_cannot_update_current_attempt(session):
    a = session.prepare("read-0")
    with pytest.raises(ValueError, match="late_attempt"):
        transition(session.plan, "read-0", "failed", attempt_id="wrong")
    with pytest.raises(ValueError, match="late_plan"):
        transition(session.plan, "read-0", "failed", attempt_id=a["attempt_id"], expected_version=7)


def test_workspace_lease_blocks_second_controller(session):
    with pytest.raises(ValueError, match="workspace_busy"):
        session_for(session.workspace)


def test_workspace_lease_is_independent_of_state_root(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with session_for(workspace, state_root=str(tmp_path / "state-a")):
        with pytest.raises(ValueError, match="workspace_busy"):
            session_for(workspace, state_root=str(tmp_path / "state-b"))
    with session_for(workspace, state_root=str(tmp_path / "state-b")):
        pass


def test_journal_and_checkpoint_corruption_reject(session):
    seal = session.checkpoint()
    with pytest.raises(ValueError, match="checksum"):
        session.store.verify_checkpoint({**seal, "state_revision": 99})
    session.store.db.execute("UPDATE events SET payload=? WHERE seq=2", (json.dumps({}),))
    session.store.db.commit()
    with pytest.raises(ValueError, match="journal_integrity"):
        session.store.events()


def test_plan_retains_observations_when_existing_store_gc_runs(session):
    from agent_runtime.context_runtime import ObservationStore

    session.run_node("read-0", lambda a: read(session, a))
    ref = session.plan.node("read-0").output_evidence_refs[0]
    state = {
        "id": session.identity["session_id"],
        "session_scope": {"session_id": session.identity["session_id"]},
    }
    observations = ObservationStore(state, session.workspace)
    try:
        observations.gc(max_records=0)
    finally:
        observations.close()
    assert session.evidence.valid(ref)
    record = session.evidence.get(ref)
    (session.store.root / "blobs" / record["blob_ref"]).write_text("corrupt")
    assert not session.evidence.valid(ref)


def test_incomplete_verify_prevents_any_new_write(session):
    through_analysis(session)
    session.run_node("edit", lambda a: write(session, a))
    session.run_node(
        "verify",
        lambda a: session.verify_result(
            a,
            {"all_passed": False},
            {"command": ["pytest"], "completed": False},
        ),
    )
    assert session.plan.node("verify").status == "uncertain"
    with pytest.raises(ValueError, match="quiescence"):
        replan(session, session.plan, reason="retry", evidence_refs=["anything"])


def test_serial_nodes_cannot_be_prepared_concurrently(session):
    through_analysis(session)
    session.prepare("edit")
    with pytest.raises(ValueError):
        session.prepare("verify")


def test_external_change_after_confirmed_patch_never_replays_it(session):
    from pathlib import Path

    through_analysis(session)
    session.run_node("edit", lambda a: write(session, a))
    (Path(session.workspace) / "value.py").write_text("external edit\n")
    report = recover(session)
    assert "edit" in report["uncertain"]
    assert session.plan.node("edit").status == "uncertain"
    assert session.plan.node("verify").status == "blocked"


def test_wrong_workspace_checkpoint_cannot_be_adopted(session, tmp_path):
    seal = session.checkpoint()
    other = tmp_path / "other"
    other.mkdir()
    with session_for(other) as restored:
        with pytest.raises(ValueError, match="identity"):
            restored.store.verify_checkpoint(seal)


def test_receipt_identity_is_validated_independently_of_journal_checksum(session):
    session.run_node("read-0", lambda a: read(session, a))
    operation = next(iter(session.store.latest("operation", "operation_id").values()))
    session.store.append(
        "operation", {**operation, "receipt": {**operation["receipt"], "run_id": "wrong"}}
    )
    with pytest.raises(ValueError, match="receipt_identity"):
        recover(session)


def test_one_parallel_read_failure_does_not_discard_other_result(tmp_path):
    from agent_runtime.tool_result import ToolResult
    from tests.plan_support import tool

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))

        def cancelled(a):
            tool(
                session,
                "read_file",
                {},
                lambda: ToolResult(
                    content="cancelled",
                    status="cancelled",
                    error_code="tool_cancelled",
                    metadata={"termination_guaranteed": False},
                ),
            )
            return session.tool_result(a)

        PlanScheduler(session).run_reads(
            {"read-0": lambda a: read(session, a), "read-1": cancelled}
        )
        assert session.plan.node("read-0").status == "succeeded"
        assert session.plan.node("read-1").status == "uncertain"
        assert session.plan.node("edit").status == "blocked"
