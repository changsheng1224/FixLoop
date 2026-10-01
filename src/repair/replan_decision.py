"""Pure, small decision boundary for verification-driven replanning."""

from dataclasses import asdict, dataclass

from agent_runtime.plan_runtime.validate import MAX_REPLANS
from src.repair.verification.verify_diagnose import VerifyBucket, diagnose_verification


@dataclass(frozen=True)
class ReplanDecision:
    action: str
    reason: str
    trigger_ref: str
    plan_id: str
    plan_version: int
    state_revision: int
    evidence_refs: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


def decide_replan(
    view,
    *,
    trigger_ref="",
    result=None,
    receipt=None,
    evidence_refs=(),
    safety_reason="",
    stop_reason="",
    retry_allowed=False,
):
    """Inputs are runtime facts; this function performs no I/O or mutation."""
    action, reason = "keep_plan", "no_confirmed_code_verification_failure"
    receipt = receipt or {}
    if safety_reason or any(n["status"] in {"running", "uncertain"} for n in view["nodes"]):
        action, reason = "block", safety_reason or "replan_requires_quiescence"
    elif stop_reason or view["plan_version"] > MAX_REPLANS:
        action, reason = "stop", stop_reason or "replan_budget_exceeded"
    elif result is not None and not result.all_passed:
        diagnosis = diagnose_verification(result)
        if not trigger_ref or receipt.get("completed") is not True or not receipt.get("command"):
            action, reason = "block", "verification_receipt_unconfirmed"
        elif result.total_tests <= 0:
            reason = "verification_empty_collection"
        elif diagnosis.bucket != VerifyBucket.LOGIC:
            reason = "verification_" + diagnosis.bucket.value
        elif not retry_allowed:
            action, reason = "stop", "orchestrator_retry_not_allowed"
        elif not evidence_refs:
            action, reason = "needs_evidence", "fresh_repository_evidence_required"
        else:
            action, reason = "replan", "confirmed_code_verification_failure"
    return ReplanDecision(
        action,
        reason,
        trigger_ref,
        view["plan_id"],
        view["plan_version"],
        view["state_revision"],
        tuple(evidence_refs),
    )
