"""Multi-Agent verify exhausted → Single-Agent baseline 降级。"""

from __future__ import annotations

import time
from typing import Protocol

from src.repair.baseline_apply import apply_baseline_answer
from src.repair.failure_tags import DEGRADED_BASELINE_TAG
from src.repair.repair_context_blocks import (
    append_degraded_context_sections,
    build_repair_context_blocks,
)
from src.repair.termination import RepairTerminalStatus, has_repair_timeout, is_repair_success
from src.state import RepairState, VerificationResult

DEGRADED_TRIGGER_VERIFY_EXHAUSTED = "verify_exhausted"


class BaselineDegradeHost(Protocol):
    """run_baseline_fallback 所需的 Orchestrator 能力面。"""

    patcher: object | None
    _repo_root: str

    def _restore_repo_snapshot(self, snapshot: dict) -> None: ...
    def _merge_blackboard_for_patch(self, state: RepairState) -> dict: ...
    def _read_code_snippet(self, file_path: str, start_line: int, end_line: int) -> str: ...
    def _read_test_context(
        self,
        context,
        suspects,
        plan,
    ) -> list[str]: ...
    def _verification_enabled(self) -> bool: ...
    def _begin_l2_agent_ask(
        self,
        state: RepairState,
        agent,
        *,
        agent_name: str,
        phase: str,
        attempt: int,
    ) -> str: ...
    def _finish_l2_agent_ask(
        self,
        state: RepairState,
        agent,
        *,
        agent_name: str,
        phase: str,
        attempt: int,
        task_id: str,
        elapsed_ms: int,
        stop_reason: str = "",
        tool_steps: int = 0,
    ) -> None: ...
    def _run_verifier(self, state: RepairState) -> VerificationResult: ...


def _had_verify_failure(state: RepairState) -> bool:
    if state.verification_result is not None and not state.verification_result.all_passed:
        return True
    return state.node_timings.get("post_patch_pytest_code") is not None


def should_degrade_to_baseline(
    state: RepairState,
    *,
    verification_enabled: bool,
    cancelled: bool,
    allow: bool = True,
) -> bool:
    """Multi-Agent 重试用尽且发生过 verify 失败时触发 baseline 最后一搏。"""
    if not allow:
        return False
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


def build_degraded_baseline_prompt(state: RepairState, host: BaselineDegradeHost | None = None) -> str:
    """构造带 Multi-Agent / Blackboard 上下文的 baseline prompt。"""
    lines = [
        "请修复以下 issue（Multi-Agent 流水线 verify 已失败 "
        f"{state.retry_count} 次，此为 Single-Agent 最后一搏）：",
        "",
        state.issue_input,
    ]

    if host is not None:
        from src.repair.run_context import RepairRunContext

        ctx = getattr(host, "_repair_ctx", None)
        blackboard = ctx.blackboard if isinstance(ctx, RepairRunContext) else None
        blocks = build_repair_context_blocks(
            state,
            blackboard=blackboard,
            read_snippet=host._read_code_snippet,
            read_test_context=host._read_test_context,
            merge_for_patch=host._merge_blackboard_for_patch,
        )
        append_degraded_context_sections(lines, blocks, state=state)
    elif state.feedback.strip():
        lines.extend(["", "## 末轮验证反馈", state.feedback.strip()])

    plan = state.repair_plan
    if plan and plan.skill.guidance:
        guidance = "\n".join(f"- {item}" for item in plan.skill.guidance[:5] if item)
        if guidance:
            lines.extend(["", "## Skill 提示", guidance])

    lines.extend(["", "可使用全部工具完成定位、修补与验证。"])
    return "\n".join(lines)


def _finalize_baseline_status(state: RepairState, *, verify_after: bool) -> None:
    if verify_after:
        if state.verification_result and state.verification_result.all_passed:
            state.status = RepairTerminalStatus.FIXED
        elif state.status != RepairTerminalStatus.FAILED:
            state.status = RepairTerminalStatus.EXHAUSTED
        return
    if is_repair_success(state):
        state.status = RepairTerminalStatus.FIXED
    elif state.status not in (RepairTerminalStatus.FIXED, RepairTerminalStatus.EXHAUSTED):
        state.status = RepairTerminalStatus.EXHAUSTED


def run_baseline_fallback(
    host: BaselineDegradeHost,
    state: RepairState,
    *,
    initial_snapshot: dict,
    verify_after: bool = True,
) -> None:
    """执行 baseline 降级并更新 *state*（in-place）。"""
    from src.agents.factory import create_baseline_agent
    from src.repair.run_context import RepairRunContext

    if host.patcher is None:
        return

    host._restore_repo_snapshot(initial_snapshot)

    agent = create_baseline_agent(
        host.patcher.model_client,
        host.patcher.workspace,
        cwd=host._repo_root,
        approval=getattr(getattr(host.patcher, "config", None), "approval", "auto"),
    )
    if state.repair_run_id:
        agent.shared_run_id = state.repair_run_id
    ctx = getattr(host, "_repair_ctx", None)
    cancel_token = ctx.cancel_token if isinstance(ctx, RepairRunContext) else None
    if cancel_token is not None:
        agent.cancel_token = cancel_token

    state.degraded_mode = True
    state.node_timings["degraded_mode"] = True
    state.node_timings["degraded_trigger"] = DEGRADED_TRIGGER_VERIFY_EXHAUSTED
    state.node_timings["multi_agent_retries"] = state.retry_count
    if DEGRADED_BASELINE_TAG not in state.failure_tags:
        state.failure_tags.append(DEGRADED_BASELINE_TAG)

    tracer = ctx.repair_tracer if isinstance(ctx, RepairRunContext) else None
    prompt = build_degraded_baseline_prompt(state, host)
    feedback_preview = (state.feedback or "")[:200]
    if tracer is not None:
        tracer.emit(
            "orchestrator",
            "repair_degraded_to_baseline",
            {
                "trigger": DEGRADED_TRIGGER_VERIFY_EXHAUSTED,
                "retry_count": state.retry_count,
                "feedback_preview": feedback_preview,
                "verify_after": verify_after,
            },
        )

    run_verify = verify_after and host._verification_enabled()
    baseline_task_id = host._begin_l2_agent_ask(
        state,
        agent,
        agent_name="baseline",
        phase="degrade",
        attempt=0,
    )

    t0 = time.time()
    try:
        apply_baseline_answer(
            agent,
            host._repo_root,
            prompt,
            state,
            mark_fixed_on_apply=not run_verify,
        )
    finally:
        elapsed_ms = int((time.time() - t0) * 1000)
        last_ts = getattr(agent, "_last_task_state", None)
        if baseline_task_id:
            host._finish_l2_agent_ask(
                state,
                agent,
                agent_name="baseline",
                phase="degrade",
                attempt=0,
                task_id=baseline_task_id,
                elapsed_ms=elapsed_ms,
                stop_reason=getattr(last_ts, "stop_reason", "") if last_ts else "",
                tool_steps=getattr(last_ts, "tool_steps", 0) if last_ts else 0,
            )

    state.node_timings["baseline_degrade_ms"] = int((time.time() - t0) * 1000)

    if (
        run_verify
        and state.candidate_patches
        and state.status != RepairTerminalStatus.FAILED
    ):
        t_verify = time.time()
        state.verification_result = host._run_verifier(state)
        state.node_timings["baseline_verify_ms"] = int((time.time() - t_verify) * 1000)
        if tracer is not None:
            tracer.emit(
                "orchestrator",
                "baseline_verify_finished",
                {
                    "all_passed": state.verification_result.all_passed,
                    "elapsed_ms": state.node_timings["baseline_verify_ms"],
                },
            )

    _finalize_baseline_status(state, verify_after=run_verify)
