"""Canonical repair failure decisions shared by feedback, trace, and resume."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RepairFailureClass(StrEnum):
    TOOL_ARGUMENTS = "tool_arguments"
    TOOL_TRANSIENT = "tool_transient"
    TOOL_POLICY = "tool_policy"
    PATCH_EMPTY = "patch_empty"
    PATCH_APPLY = "patch_apply"
    VERIFY_LOGIC = "verify_logic"
    VERIFY_COLLECTION = "verify_collection"
    VERIFY_ENVIRONMENT = "verify_environment"
    VERIFY_UNKNOWN = "verify_unknown"


@dataclass(frozen=True)
class RepairFailureDecision:
    failure_class: str
    retryable: bool
    next_action: str
    model_hint: str
    evidence_refs: list[str] = field(default_factory=list)
    retry_limit: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_verification_failure(result, *, state=None) -> RepairFailureDecision:
    from src.repair.verification.verify_diagnose import VerifyBucket, diagnose_verification

    diagnosis = diagnose_verification(result)
    nodeids = list(diagnosis.failed_nodeids)
    if state is not None:
        nodeids = list(state.node_timings.get("verify_failed_nodeids") or nodeids)
    if diagnosis.bucket == VerifyBucket.ENV:
        return RepairFailureDecision(
            RepairFailureClass.VERIFY_ENVIRONMENT.value,
            False,
            "repair_verification_environment",
            "Do not modify business logic until the verification environment is usable.",
            nodeids[:8],
        )
    if diagnosis.bucket == VerifyBucket.COLLECT:
        return RepairFailureDecision(
            RepairFailureClass.VERIFY_COLLECTION.value,
            True,
            "repair_test_collection_or_entrypoint",
            "Fix the import, test target, or collection entrypoint before changing logic.",
            nodeids[:8],
            1,
        )
    if diagnosis.bucket == VerifyBucket.LOGIC:
        return RepairFailureDecision(
            RepairFailureClass.VERIFY_LOGIC.value,
            not bool(getattr(result, "all_passed", False)),
            "read_failed_test_patch_minimal_impl_reverify_same_target",
            "Read the failing assertion, revise the active hypothesis, and make a minimal patch.",
            nodeids[:8],
            2,
        )
    return RepairFailureDecision(
        RepairFailureClass.VERIFY_UNKNOWN.value,
        True,
        "inspect_verification_evidence_before_patch",
        "Classify the failure from logs before making another code change.",
        nodeids[:8],
        1,
    )


def decide_tool_failure(metadata: dict[str, Any] | None) -> RepairFailureDecision:
    meta = metadata or {}
    code = str(meta.get("tool_error_code") or "tool_execution_failed")
    if code in {"invalid_args", "invalid_arguments"}:
        failure = RepairFailureClass.TOOL_ARGUMENTS
        action = "correct_tool_arguments_from_schema"
    elif code in {"permission_denied", "policy_denied", "approval_denied"}:
        failure = RepairFailureClass.TOOL_POLICY
        action = "choose_authorized_tool_or_satisfy_precondition"
    else:
        failure = RepairFailureClass.TOOL_TRANSIENT
        action = "retry_narrower_or_continue_without_tool"
    return RepairFailureDecision(
        failure.value,
        bool(meta.get("retryable", False)),
        action,
        str(meta.get("model_hint") or "Revise the tool plan using available capabilities."),
        [str(meta["observation_id"])] if meta.get("observation_id") else [],
        int(meta.get("retry_limit", 0) or 0),
    )


def apply_failure_decision(state, decision: RepairFailureDecision) -> None:
    data = decision.to_dict()
    state.node_timings["repair_failure_decision"] = data
    state.node_timings["required_next_action"] = decision.next_action
    state.agent_errors["repair_failure_class"] = decision.failure_class


def render_failure_decision(decision: RepairFailureDecision | dict) -> str:
    data = decision.to_dict() if isinstance(decision, RepairFailureDecision) else dict(decision)
    return (
        "[REPAIR FAILURE DECISION]\n"
        f"class={data.get('failure_class', '')}; "
        f"retryable={str(bool(data.get('retryable'))).lower()}; "
        f"next_action={data.get('next_action', '')}\n"
        f"guidance={data.get('model_hint', '')}"
    )
