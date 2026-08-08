import json
import time

import pytest

from src.blackboard import Blackboard
from src.collaboration_governance import atomic_collaboration_update
from src.state import RepairState, RepairStatus


def test_repairstate_migrates_v1_and_rejects_unknown_schema():
    state = RepairState.from_dict({"issue_input": "x", "schema_version": "1.0"})
    assert state.schema_version == "1.1"
    assert state.status == RepairStatus.PENDING
    with pytest.raises(ValueError, match="unsupported"):
        RepairState.from_dict({"issue_input": "x", "schema_version": "9.0"})


def test_repairstate_invariants_are_checked_on_commit():
    state = RepairState(issue_input="x", phase="done", status="fixed")
    with pytest.raises(ValueError, match="candidate_patches"):
        state.validate_invariants(strict=True)


def test_blackboard_snapshot_preserves_entry_metadata_and_isolated_values():
    board = Blackboard()
    board.write("scratch:x", {"items": [1]}, "patcher", ttl=30, evidence_refs=["E1"])
    snapshot = board.snapshot()
    snapshot["entries"]["scratch:x"]["items"].append(2)
    restored = Blackboard()
    restored.restore_snapshot(snapshot)
    assert restored.read("scratch:x") == {"items": [1]}
    record = restored.snapshot()["entry_records"][0]
    assert record["source_agent"] == "patcher"
    assert record["evidence_refs"] == ["E1"]
    assert record["ttl"] == 30


def test_blackboard_per_key_cas_allows_disjoint_proposals():
    board = Blackboard()
    first = board.merge_proposal(board.propose("a", 1, "localizer", base_entry_revision=0))
    assert first["status"] == "accepted"
    second = board.merge_proposal(board.propose("b", 2, "retriever", base_entry_revision=0))
    assert second["status"] == "accepted"
    assert board.read("a") == 1
    assert board.read("b") == 2


def test_blackboard_namespace_policy_rejects_unauthorized_source():
    board = Blackboard()
    board.register_namespace("suspect:", allowed_sources={"localizer"})
    assert not board.write("suspect:x", {}, "verifier")
    assert board.conflicts[-1]["status"] == "rejected"


def test_blackboard_conflict_strategies_support_priority_merge_and_reject():
    from src.repair.blackboard_merge import resolve_blackboard_conflicts

    board = Blackboard()
    board.write("context:x", ["a"], "retriever")
    assert not board.write("context:x", ["b"], "verifier")
    resolved = resolve_blackboard_conflicts(board, strategy="trusted_source_priority")
    assert resolved[0]["winner_source"] == "verifier"
    assert board.read("context:x") == ["b"]

    board.write("context:y", ["a"], "retriever")
    assert not board.write("context:y", ["b"], "verifier")
    resolve_blackboard_conflicts(board, strategy="reject_all")
    assert board.conflicts == []


def test_blackboard_ttl_is_not_restored_after_expiry():
    board = Blackboard()
    board.write("scratch:x", "v", "patcher", ttl=0.01)
    time.sleep(0.03)
    restored = Blackboard()
    restored.restore_snapshot(board.snapshot())
    assert restored.read("scratch:x") is None


def test_atomic_collaboration_update_rolls_back_board_on_conflict():
    state = RepairState(issue_input="x", field_owners={"feedback": "patcher"})
    board = Blackboard()
    board.write("scratch:feedback", "old", "verifier")
    result = atomic_collaboration_update(
        state,
        board,
        {"feedback": "new"},
        actor="patcher",
        expected_revision=0,
        writes=[{"key": "scratch:feedback", "value": "new", "source_agent": "patcher"}],
    )
    assert not result["accepted"]
    assert state.feedback == ""
    assert board.read("scratch:feedback") == "old"


def test_repair_checkpoint_rejects_top_level_tampering(tmp_path):
    from src.repair.checkpoint_load import load_repair_checkpoint, save_repair_checkpoint

    state = RepairState(issue_input="x", repair_run_id="run-1")
    path = save_repair_checkpoint(state, str(tmp_path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["feedback"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_repair_checkpoint(str(tmp_path), "run-1") is None
