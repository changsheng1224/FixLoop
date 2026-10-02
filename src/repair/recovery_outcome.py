"""Detached recovery/cancel diagnostics; never an execution authorization."""

from copy import deepcopy

TERMINAL_RESOURCES = {"completed", "failed", "cancelled", "not_started"}
ACTION_TEXT = {
    "inspect_checkpoint": "核对 checkpoint 的完整性、任务与工作区身份；修复前不要续跑。",
    "wait": "等待当前执行者或取消清理完成，再核对状态。",
    "verify_execution": "核验所列资源的退出收据及工作区后态，再显式发起恢复核查。",
    "new_run": "本次运行已终结；如需继续工作，请使用新的 run ID。",
    "inspect_recovery": "检查恢复阻断原因及关联节点，保留工作区和恢复记录。",
    "recheck_resume": "再次恢复必须重新取得 owner 并核验当前执行事实。",
    "continue_runtime": "恢复核查完成；后续派发仍须通过运行时门禁。",
}


def build_recovery_outcome(source, *, stage, plan_report=None, reason_code=""):
    resources = [
        {
            "resource_id": r.get("resource_id", ""),
            "kind": r.get("kind", ""),
            "effect": r.get("effect", ""),
            "status": r.get("status", "unknown"),
            "cleanup": r.get("cleanup", "unknown"),
            "reason_code": r.get("error_code", r.get("reason_code", "")),
            "receipt_ref": r.get("receipt_ref", ""),
        }
        for r in source.get("resources", [])
    ]
    blocking = [
        r for r in resources if r["status"] not in TERMINAL_RESOURCES or r["cleanup"] != "confirmed"
    ]
    plan = {
        key: list((plan_report or {}).get(key, []))
        for key in ("adopted", "restarted_read", "rerun_verify", "stale", "uncertain", "blocked")
    }
    status = source.get("status", "recovery_required")
    reason = reason_code or next((r["reason_code"] for r in blocking if r["reason_code"]), "")
    action = "recheck_resume"
    if stage == "checkpoint":
        action = "inspect_checkpoint"
    elif status == "resume_owner_conflict" or status in {
        "cancel_requested",
        "cancelling",
        "reconciling",
    }:
        action = "wait"
    elif blocking or plan["uncertain"] or reason == "resume_untracked_execution_uncertain":
        action = "verify_execution"
    elif status in {"cancelled", "failed"}:
        action = "new_run"
    elif status == "recovery_required":
        action = "inspect_recovery"
    elif status == "active" and stage == "context":
        action = "continue_runtime"
    effects_verified = None
    if (
        plan["uncertain"]
        or plan["blocked"]
        or reason in {"resume_untracked_execution_uncertain", "repair_worker_unconfirmed"}
    ):
        effects_verified = False
    elif plan_report is not None and plan["adopted"] and not blocking:
        effects_verified = True
    return deepcopy(
        {
            "run_id": source.get("run_id", ""),
            "generation": source.get("generation", 0),
            "coordination_revision": source.get("coordination_revision", 0),
            "stage": stage,
            "status": status,
            "reason_code": reason,
            "cancel_requested": bool(source.get("cancel_request_id")),
            "cleanup_confirmed": stage not in {"checkpoint", "owner"}
            and status != "cancel_requested"
            and reason != "repair_worker_unconfirmed"
            and not blocking,
            # A stopped process alone says nothing about workspace postconditions.
            "effects_verified": effects_verified,
            "effects_scope": "recovered_plan_attempts",
            "resources": resources,
            "blocking_resources": blocking,
            "plan": plan,
            "next_action": action,
            "guidance": ACTION_TEXT[action],
        }
    )


def publish_recovery_outcome(
    state, source, *, stage, emitter=None, plan_report=None, reason_code=""
):
    outcome = build_recovery_outcome(
        source, stage=stage, plan_report=plan_report, reason_code=reason_code
    )
    state.control.recovery_outcome = outcome
    if emitter is not None:
        emitter.emit(
            "recovery_progress",
            summary=f"{outcome['status']}: {outcome['reason_code'] or outcome['stage']}",
            recovery=deepcopy(outcome),
        )
    return deepcopy(outcome)
