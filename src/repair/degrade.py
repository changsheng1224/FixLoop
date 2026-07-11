"""Multi-Agent verify  exhausted → Single-Agent baseline 降级。"""

from __future__ import annotations

import time

from src.repair.baseline_apply import apply_baseline_answer, snapshot_baseline_sources
from src.repair.termination import RepairTerminalStatus, has_repair_timeout, is_repair_success
from src.state import RepairState, SuspectLocation

DEGRADED_TRIGGER_VERIFY_EXHAUSTED = "verify_exhausted"


def _had_verify_failure(state: RepairState) -> bool:
    if state.verification_result is not None and not state.verification_result.all_passed:
        return True
    return state.node_timings.get("post_patch_pytest_code") is not None


def should_degrade_to_baseline(
    state: RepairState,
    *,
    verification_enabled: bool,
    cancelled: bool,
) -> bool:
    """Multi-Agent 重试用尽且发生过 verify 失败时触发 baseline 最后一搏。"""
    if not verification_enabled:
        return False
    if cancelled or state.node_timings.get("user_cancel"):
        return False
    if is_repair_success(state):
        return False
    if has_repair_timeout(state):
        return False
    if state.retry_count < state.max_retries:
        return False
    return _had_verify_failure(state)


def build_degraded_baseline_prompt(state: RepairState) -> str:
    """构造带 Multi-Agent 上下文的 baseline prompt。"""
    lines = [
        "请修复以下 issue（Multi-Agent 流水线 verify 已失败 "
        f"{state.retry_count} 次，此为 Single-Agent 最后一搏）：",
        "",
        state.issue_input,
    ]
    suspect_lines = _format_suspects(state.suspect_locations)
    if suspect_lines:
        lines.extend(["", "## 已定位嫌疑", suspect_lines])
    if state.feedback.strip():
        lines.extend(["", "## 末轮验证反馈", state.feedback.strip()])
    lines.extend(["", "可使用全部工具完成定位、修补与验证。"])
    return "\n".join(lines)


def _format_suspects(suspects: list[SuspectLocation]) -> str:
    if not suspects:
        return ""
    parts = []
    for suspect in suspects[:8]:
        parts.append(
            f"- {suspect.file_path}:{suspect.start_line}-{suspect.end_line} "
            f"({suspect.reason})"
        )
    return "\n".join(parts)


def run_baseline_fallback(orch, state: RepairState, *, initial_snapshot: dict) -> None:
    """执行 baseline 降级并更新 *state*（in-place）。"""
    from src.agents.factory import create_baseline_agent

    if orch.patcher is None:
        return

    orch._restore_repo_snapshot(initial_snapshot)

    agent = create_baseline_agent(
        orch.patcher.model_client,
        orch.patcher.workspace,
        cwd=orch._repo_root,
        approval=getattr(getattr(orch.patcher, "config", None), "approval", "auto"),
    )
    if state.repair_run_id:
        agent.shared_run_id = state.repair_run_id

    state.degraded_mode = True
    state.node_timings["degraded_mode"] = True
    state.node_timings["degraded_trigger"] = DEGRADED_TRIGGER_VERIFY_EXHAUSTED
    state.node_timings["multi_agent_retries"] = state.retry_count

    tracer = getattr(orch, "_repair_tracer", None)
    prompt = build_degraded_baseline_prompt(state)
    feedback_preview = (state.feedback or "")[:200]
    if tracer is not None:
        tracer.emit(
            "orchestrator",
            "repair_degraded_to_baseline",
            {
                "trigger": DEGRADED_TRIGGER_VERIFY_EXHAUSTED,
                "retry_count": state.retry_count,
                "feedback_preview": feedback_preview,
            },
        )

    t0 = time.time()
    apply_baseline_answer(agent, orch._repo_root, prompt, state)
    state.node_timings["baseline_degrade_ms"] = int((time.time() - t0) * 1000)
    if is_repair_success(state):
        state.status = RepairTerminalStatus.FIXED
    elif state.status not in (RepairTerminalStatus.FIXED, RepairTerminalStatus.EXHAUSTED):
        state.status = RepairTerminalStatus.EXHAUSTED
