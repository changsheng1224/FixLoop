from __future__ import annotations


def test_blackboard_proposal_requires_current_revision():
    from src.blackboard import Blackboard

    board = Blackboard()
    proposal = board.propose("hypothesis", "a", "patcher")
    accepted = board.merge_proposal(proposal)
    assert accepted["status"] == "accepted"
    stale = board.propose("other", "b", "verifier", base_revision=0)
    rejected = board.merge_proposal(stale)
    assert rejected["status"] == "stale"
    assert board.read("other") is None


def test_blackboard_same_value_merges_evidence():
    from src.blackboard import Blackboard

    board = Blackboard()
    first = board.merge_proposal(board.propose("file", "a.py", "patcher", evidence_refs=["E1"]))
    second = board.merge_proposal(
        board.propose("file", "a.py", "verifier", evidence_refs=["E2"])
    )
    assert first["status"] == "accepted"
    assert second["status"] == "accepted"


def test_tool_policy_uses_role_mode_and_phase():
    from src.collaboration_governance import CollaborationGovernance

    policy = CollaborationGovernance()
    assert not policy.authorize("apply_patch", role="patcher", phase="context", evidence=True).allowed
    assert not policy.authorize("apply_patch", role="verifier", phase="patch", evidence=True).allowed
    assert policy.authorize("apply_patch", role="patcher", phase="patch", evidence=True).allowed


def test_role_lifecycle_and_projection_are_revisioned():
    from src.collaboration_governance import CollaborationGovernance, RoleLifecycle
    from src.state import RepairState

    state = RepairState(issue_input="fix", repair_run_id="r1")
    event = CollaborationGovernance.lifecycle_transition(
        state, "patcher", RoleLifecycle.ACTIVE, "patch phase"
    )
    assert event["to"] == "active"
    assert state.active_roles == ["patcher"]
    projection = CollaborationGovernance.apply_state_projection(state, "patcher")
    assert projection["role"] == "patcher"
    assert projection["state_revision"] == 1


def test_state_patch_enforces_revision_and_owner():
    from src.collaboration_governance import apply_state_patch
    from src.state import RepairState

    state = RepairState(issue_input="fix", field_owners={"feedback": "patcher"})
    denied = apply_state_patch(state, {"feedback": "x"}, actor="verifier", expected_revision=0)
    assert denied["reason"] == "field_owner_mismatch"
    stale = apply_state_patch(state, {"feedback": "x"}, actor="patcher", expected_revision=2)
    assert stale["status"] == "stale"
    accepted = apply_state_patch(state, {"feedback": "x"}, actor="patcher", expected_revision=0)
    assert accepted["accepted"] and state.state_revision == 1


def test_skill_policy_is_intersection_only():
    from src.collaboration_governance import CollaborationGovernance, sanitize_skill_policy

    result = sanitize_skill_policy(
        ["read_file", "apply_patch", "run_shell"],
        CollaborationGovernance(),
        role="patcher",
        phase="patch",
        trust_level="untrusted",
    )
    assert result["suggested_tools"] == ["read_file", "apply_patch"]
    assert result["guidance_only"] is True


def test_skill_metadata_is_traceable():
    from src.skills.models import MatchedSkill, SkillSpec

    spec = SkillSpec(
        name="python_fix",
        trigger_pattern="TypeError",
        guidance=["inspect", "verify"],
        source="workspace_local",
        trust_level="untrusted",
        version="2",
    )
    matched = MatchedSkill.from_spec(spec)
    payload = matched.to_trace_payload()
    assert payload["source"] == "workspace_local"
    assert payload["trust_level"] == "untrusted"
