from __future__ import annotations

from agent_runtime.harness_engineering import (
    ExecutionContract,
    HarnessControlPlane,
    HarnessFailureCode,
    HarnessStatus,
    attribute_harness_failure,
    extract_bad_case,
)
from src.collaboration_governance import CollaborationGovernance
from src.state import RepairState
from src.tools.spec import default_repair_tool_registry, project_tool_specs


def test_control_plane_lifecycle_budget_and_terminal_race():
    events = []
    control = HarnessControlPlane(
        "run-1", max_attempts=2, budget_limits={"write": 1}, event_sink=events.append
    )
    assert control.start(ExecutionContract(run_id="run-1"))
    assert control.record_phase("patch")
    assert control.reserve({"write": 1}, reservation_id="write-1").allowed
    assert not control.reserve({"write": 1}, reservation_id="write-2").allowed
    assert control.finish(HarnessStatus.COMPLETED)
    assert not control.finish(HarnessStatus.FAILED)
    assert any(event["event_type"] == "run_terminal_late" for event in events)


def test_failure_attribution_roundtrip_to_bad_case():
    attribution = attribute_harness_failure(
        code=HarnessFailureCode.VERIFICATION_FAILED,
        evidence_refs=["trace:run-2"],
    )
    snapshot = {
        "run_id": "run-2",
        "status": HarnessStatus.FAILED.value,
        "failure": attribution.to_dict(),
        "manifest_fingerprint": "manifest-2",
        "trace_refs": ["trace:run-2"],
    }
    record = extract_bad_case(snapshot)
    assert record is not None
    assert record.primary_cause == HarnessFailureCode.VERIFICATION_FAILED
    assert record.manifest_fingerprint == "manifest-2"
    assert record.close(rerun_run_id="run-3", manifest_fingerprint="manifest-2", reason="fixed")


def test_state_persists_harness_fields_and_tool_approval_policy():
    state = RepairState(issue_input="issue")
    state.harness_control = {"status": "running", "run_id": "run-3"}
    state.harness_metrics = {"event_count": 1}
    state.harness_manifest = {"manifest_fingerprint": "m-3"}
    restored = RepairState.from_dict(state.to_dict())
    assert restored.harness_manifest["manifest_fingerprint"] == "m-3"

    registry = default_repair_tool_registry()
    policy = CollaborationGovernance(registry=registry)
    denied = policy.authorize(
        "apply_patch",
        role="patcher",
        phase="patch",
        evidence=True,
        read_before_write=True,
        control_mode="approval_required",
    )
    assert not denied.allowed
    assert denied.reason == "human_approval_required"
    projected = project_tool_specs([registry.get("apply_patch")])["apply_patch"]
    assert projected["risk_level"] == "high"
    assert projected["requires_approval"] is True
