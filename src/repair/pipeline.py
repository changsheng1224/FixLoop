"""修复流水线 Template Method（从 Orchestrator 提取）。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.eval.runner import run_pytest
from src.repair.degrade import run_baseline_fallback, should_degrade_to_baseline
from src.repair.termination import (
    RepairTerminalStatus,
    finalize_repair_state,
    mark_fixed_skip_verify,
)
from src.repair.output_parsers import parse_retrieved_context, parse_suspect_list
from src.repair.blackboard_merge import (
    BLACKBOARD_SCHEMA_VERSION,
    merge_blackboard_for_patch,
    write_feedback_to_blackboard,
    write_localize_phase_to_blackboard,
)
from src.repair.l2_binding import (
    AgentAskRef,
    bind_l2_context,
    clear_l2_context,
    make_repair_task_id,
)
from src.repair.phase_clock import PhaseTimeoutError, RepairPhaseClock
from src.repair.timing_schema import (
    finalize_phases,
    set_parallel_wall_ms,
    set_phase_ms,
    set_repair_total_ms,
)
from src.repair.prompt_router import repair_plan_intent_snapshot
from src.state import RepairState, RetrievedContext, SuspectLocation

log = get_logger("repair.pipeline")


def _record_pytest_exit(state: RepairState, repo_root: str, key: str) -> None:
    code, _ = run_pytest(Path(repo_root))
    state.node_timings[key] = code


class RepairPipelineMixin:
    """Orchestrator 修复主循环与 Localizer/Retriever 步骤。"""

    def _make_phase_clock(self) -> RepairPhaseClock | None:
        config = getattr(self, "_phase_timeout_config", None)
        if config is None or not config.any_enabled():
            return None
        return RepairPhaseClock(config)

    def _apply_phase_timeout(
        self,
        state: RepairState,
        initial_snapshot: dict,
        exc: PhaseTimeoutError,
    ) -> None:
        token = getattr(self, "_cancel_token", None)
        if token is not None:
            token.cancel("timeout")
        self._restore_repo_snapshot(initial_snapshot)
        state.status = RepairTerminalStatus.TIMEOUT
        state.node_timings["phase_timeout"] = exc.phase
        config = getattr(self, "_phase_timeout_config", None)
        if config is not None:
            state.node_timings["phase_timeout_budgets"] = config.budget_dict()
        state.node_timings["phase_timeout_consumed_s"] = exc.consumed_s
        state.agent_errors["orchestrator"] = str(exc)
        tracer = self._repair_tracer
        if tracer is not None:
            tracer.emit(
                "orchestrator",
                "phase_timeout",
                {
                    "phase": exc.phase,
                    "budget_s": exc.budget_s,
                    "consumed_s": exc.consumed_s,
                },
            )
        log.warning("阶段超时: %s", exc)

    def _l2_elapsed_ms(self) -> int:
        started = getattr(self, "_repair_started_at", None)
        if started is None:
            return 0
        return int((time.time() - started) * 1000)

    def _begin_l2_agent_ask(
        self,
        state: RepairState,
        agent,
        *,
        agent_name: str,
        phase: str,
        attempt: int,
    ) -> str:
        repair_run_id = state.repair_run_id
        if not repair_run_id or agent is None:
            return ""
        started_ms = self._l2_elapsed_ms()
        task_id = bind_l2_context(
            agent,
            repair_run_id=repair_run_id,
            agent_name=agent_name,
            phase=phase,
            attempt=attempt,
            started_ms=started_ms,
        )
        tracer = self._repair_tracer
        if tracer is not None:
            tracer.emit(
                agent_name,
                "agent_ask_started",
                {
                    "task_id": task_id,
                    "repair_run_id": repair_run_id,
                    "l2_agent": agent_name,
                    "l2_phase": phase,
                    "l2_attempt": attempt,
                    "started_ms": started_ms,
                },
            )
        return task_id

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
    ) -> None:
        if not task_id or not state.repair_run_id:
            clear_l2_context(agent)
            return
        finished_ms = self._l2_elapsed_ms()
        started_ms = int(getattr(agent, "_l2_ask_started_ms", finished_ms - elapsed_ms))
        ref = AgentAskRef(
            agent=agent_name,
            phase=phase,
            attempt=int(attempt),
            task_id=task_id,
            run_id=state.repair_run_id,
            started_ms=started_ms,
            finished_ms=finished_ms,
            stop_reason=stop_reason,
            tool_steps=int(tool_steps),
        )
        state.agent_asks.append(ref)
        tracer = self._repair_tracer
        if tracer is not None:
            tracer.emit(
                agent_name,
                "agent_ask_finished",
                {
                    **ref.to_dict(),
                    "elapsed_ms": elapsed_ms,
                },
            )
        clear_l2_context(agent)

    def _record_l2_synthetic_ask(
        self,
        state: RepairState,
        *,
        agent_name: str,
        phase: str,
        attempt: int,
        elapsed_ms: int,
        stop_reason: str = "",
        tool_steps: int = 0,
    ) -> str:
        """Patcher complete_once / Verifier 等非 AgentLoop 路径。"""
        repair_run_id = state.repair_run_id
        if not repair_run_id:
            return ""
        task_id = make_repair_task_id(repair_run_id, agent_name, attempt)
        finished_ms = self._l2_elapsed_ms()
        started_ms = max(0, finished_ms - int(elapsed_ms))
        ref = AgentAskRef(
            agent=agent_name,
            phase=phase,
            attempt=int(attempt),
            task_id=task_id,
            run_id=repair_run_id,
            started_ms=started_ms,
            finished_ms=finished_ms,
            stop_reason=stop_reason,
            tool_steps=int(tool_steps),
        )
        tracer = self._repair_tracer
        if tracer is not None:
            payload = {
                "task_id": task_id,
                "repair_run_id": repair_run_id,
                "l2_agent": agent_name,
                "l2_phase": phase,
                "l2_attempt": attempt,
                "started_ms": started_ms,
                "synthetic": True,
            }
            tracer.emit(agent_name, "agent_ask_started", payload)
            tracer.emit(
                agent_name,
                "agent_ask_finished",
                {**ref.to_dict(), "elapsed_ms": elapsed_ms, "synthetic": True},
            )
        state.agent_asks.append(ref)
        return task_id

    def _init_repair_blackboard(self) -> None:
        from src.blackboard import Blackboard

        self._blackboard = Blackboard()

    def _write_localize_phase_to_blackboard(
        self,
        state: RepairState,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
    ) -> dict:
        """Write localize/retrieve outputs to Blackboard (merge deferred to patch)."""
        bb = getattr(self, "_blackboard", None)
        if bb is None:
            state.suspect_locations = suspects
            state.retrieved_context = context or RetrievedContext()
            return {"suspects_written": len(suspects), "context_keys_written": 0}

        write_stats = write_localize_phase_to_blackboard(bb, suspects, context)
        tracer = self._repair_tracer
        if tracer is not None:
            tracer.emit(
                "orchestrator",
                "blackboard_written",
                {**write_stats, "phase": "localize"},
            )
            tracer.emit(
                "orchestrator",
                "blackboard_snapshot",
                bb.snapshot(),
            )
        return write_stats

    def _merge_blackboard_for_patch(self, state: RepairState) -> dict:
        """Read Blackboard at patch boundary and materialize into RepairState."""
        bb = getattr(self, "_blackboard", None)
        if bb is None:
            return {}

        merge_meta = merge_blackboard_for_patch(state, bb)
        tracer = self._repair_tracer
        if tracer is not None:
            tracer.emit(
                "orchestrator",
                "blackboard_merge_for_patch",
                {
                    "suspect_count": merge_meta["suspect_count"],
                    "context_keys": merge_meta["context_keys"],
                    "conflict_count": len(merge_meta["conflicts"]),
                    "conflicts_resolved": merge_meta["conflicts_resolved"],
                    "scratch_feedback_applied": merge_meta["scratch_feedback_applied"],
                    "retry_count": merge_meta["retry_count"],
                    "blackboard_schema_version": BLACKBOARD_SCHEMA_VERSION,
                },
            )
            if merge_meta["conflicts"]:
                tracer.emit(
                    "orchestrator",
                    "blackboard_conflicts",
                    {"conflicts": merge_meta["conflicts"]},
                )
            tracer.emit(
                "orchestrator",
                "blackboard_snapshot",
                merge_meta["snapshot"],
            )
        return merge_meta

    def _write_feedback_to_blackboard(self, feedback: str) -> None:
        bb = getattr(self, "_blackboard", None)
        if bb is not None:
            write_feedback_to_blackboard(bb, feedback)

    def _sync_localize_phase_via_blackboard(
        self,
        state: RepairState,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
    ) -> dict:
        """Write-only blackboard sync after localize (patch merge reads BB later)."""
        return self._write_localize_phase_to_blackboard(state, suspects, context)

    def _run_localizer_only(
        self,
        state: RepairState,
    ) -> tuple[list[SuspectLocation], RetrievedContext, dict, dict]:
        plan = state.repair_plan
        issue = state.issue_input
        prompt = self._localizer_prompt(plan, issue)
        answer, loc_timing = self._run_agent(
            self.localizer,
            prompt,
            "localizer",
            state,
            l2_phase="localize",
            l2_attempt=0,
        )
        suspects = parse_suspect_list(answer)
        if not suspects:
            suspects = self._fallback_suspects_from_plan(plan, issue)
        empty_ctx = RetrievedContext()
        ret_timing = {"total_ms": 0, "internal": {}}
        return suspects, empty_ctx, loc_timing, ret_timing

    def _run_localize_and_retrieve(
        self,
        state: RepairState,
    ) -> tuple[list[SuspectLocation], RetrievedContext, dict, dict]:
        if self.retriever is None:
            return self._run_localizer_only(state)

        plan = state.repair_plan
        issue = state.issue_input

        def run_localizer():
            prompt = self._localizer_prompt(plan, issue)
            answer, timing = self._run_agent(
                self.localizer,
                prompt,
                "localizer",
                state,
                l2_phase="localize",
                l2_attempt=0,
            )
            suspects = parse_suspect_list(answer)
            if not suspects:
                log.warning(
                    "[localizer] 0 suspects, raw[:500]=%r",
                    answer.strip()[:500],
                )
            return suspects, timing

        def run_retriever():
            prompt = self._retriever_prompt([], plan=plan, issue=issue)
            answer, timing = self._run_agent(
                self.retriever,
                prompt,
                "retriever",
                state,
                l2_phase="retrieve",
                l2_attempt=0,
            )
            return parse_retrieved_context(answer), timing

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_loc = pool.submit(run_localizer)
            fut_ret = pool.submit(run_retriever)
            suspects, loc_timing = fut_loc.result()
            context, ret_timing = fut_ret.result()

        if not suspects:
            suspects = self._fallback_suspects_from_plan(plan, issue)
            if suspects:
                log.info(
                    "[localizer] 降级: RepairPlan → %d suspect",
                    len(suspects),
                )

        return suspects, context, loc_timing, ret_timing

    def _repair_impl(
        self,
        state: RepairState,
        initial_snapshot: dict | None = None,
    ) -> RepairState:
        """修复流水线主体（可被 repair() 超时包装）。"""
        if initial_snapshot is None:
            initial_snapshot = self._snapshot_repo()

        max_retries = state.max_retries
        issue = state.issue_input

        t_start = time.time()
        self._repair_started_at = t_start
        self._reset_token_tracking()
        self._begin_repair_trace(state)
        self._init_repair_blackboard()
        log.info("Orchestrator 开始")

        cancelled = False
        phase_timed_out = False
        phase_clock = self._make_phase_clock()
        try:
            if self._abort_repair_if_cancelled(state):
                cancelled = True
            else:
                t0 = time.time()
                state.repair_plan = self._parse_issue(issue)
                parse_ms = int((time.time() - t0) * 1000)
                if state.repair_plan and state.repair_plan.language != "python":
                    log.warning(
                        "检测到 language=%s（%s），当前 Verifier 仅支持 Python 修复",
                        state.repair_plan.language,
                        state.repair_plan.language_source,
                    )
                t_skill = time.time()
                from src.skills.resolve import resolve_skill_for_plan, skill_matched_trace_payload

                matched = None
                skill_fallback = None
                if state.repair_plan:
                    matched, skill_fallback = resolve_skill_for_plan(
                        state.repair_plan,
                        issue,
                        language=state.repair_plan.language,
                        match_skill_fn=self._match_skill,
                    )
                skill_ms = int((time.time() - t_skill) * 1000)
                tracer = self._repair_tracer
                if state.repair_plan and tracer is not None:
                    tracer.emit(
                        "orchestrator",
                        "prompt_routing",
                        repair_plan_intent_snapshot(state.repair_plan),
                    )
                    tracer.emit(
                        "orchestrator",
                        "skill_matched",
                        skill_matched_trace_payload(matched, skill_fallback)
                        if skill_fallback is not None
                        else {"matched_skill": None},
                    )
                state.node_timings["parse_issue_ms"] = parse_ms
                state.node_timings["skill_resolve_ms"] = skill_ms
                log.info("parse_issue: %dms, skill_resolve: %dms", parse_ms, skill_ms)

                log.info("Localizer + Retriever 并行开始...")
                if phase_clock is not None:
                    phase_clock.ensure("localize")
                t0 = time.time()
                suspects, context, loc_timing, ret_timing = self._run_localize_and_retrieve(state)
                wall_ms = int((time.time() - t0) * 1000)
                if phase_clock is not None:
                    phase_clock.consume("localize", wall_ms)
                set_parallel_wall_ms(state.node_timings, wall_ms)
                set_phase_ms(
                    state.node_timings,
                    "localize",
                    loc_timing["total_ms"],
                    internal=loc_timing["internal"],
                )
                set_phase_ms(
                    state.node_timings,
                    "retrieve",
                    ret_timing["total_ms"],
                    internal=ret_timing["internal"],
                )
                write_stats = self._sync_localize_phase_via_blackboard(state, suspects, context)
                n = write_stats.get("suspects_written", len(suspects))
                n_tests = len(context.related_tests) if context else 0
                log.info(
                    "Localizer+Retriever 完成: 墙钟%dms (L=%dms, R=%dms), %d suspect, %d tests",
                    wall_ms,
                    loc_timing["total_ms"],
                    ret_timing["total_ms"],
                    n,
                    n_tests,
                )

                if self._abort_repair_if_cancelled(state):
                    cancelled = True
                elif self._verification_enabled():
                    _record_pytest_exit(state, self._repo_root, "baseline_pytest_code")

                while not cancelled and state.retry_count < max_retries:
                    if self._abort_repair_if_cancelled(state):
                        cancelled = True
                        break

                    repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
                    log.info("Patcher 开始 (retry=%d)...", state.retry_count)
                    if phase_clock is not None:
                        phase_clock.ensure("patch")
                    state.candidate_patches, patch_timing = self._run_patcher(state)
                    if phase_clock is not None:
                        phase_clock.consume("patch", patch_timing["total_ms"])
                    if patch_timing.get("user_cancel") or self._abort_repair_if_cancelled(state):
                        cancelled = True
                        break

                    set_phase_ms(
                        state.node_timings,
                        "patch",
                        patch_timing["total_ms"],
                        internal={
                            "model_call_ms": patch_timing["model_call_ms"],
                            "parse_apply_ms": patch_timing["parse_apply_ms"],
                        },
                    )
                    ms = patch_timing["total_ms"]
                    n = len(state.candidate_patches)
                    log.info("Patcher 完成: %dms, %d个补丁", ms, n)

                    if not state.candidate_patches and not state.agent_errors.get("patcher_apply"):
                        state.node_timings["patcher_parse_failed"] = True

                    if not self._verification_enabled():
                        if state.candidate_patches:
                            mark_fixed_skip_verify(state)
                        break

                    if not state.candidate_patches:
                        if state.agent_errors.pop("patcher_apply", None):
                            state.feedback = (
                                "补丁 JSON 解析成功但未能写入文件。"
                                "original_lines 必须与预读代码完全一致；优先使用 diff 字段。"
                            )
                        else:
                            state.feedback = "补丁生成失败（模型返回无法解析的 JSON）。请重新生成。"
                        self._write_feedback_to_blackboard(state.feedback)
                        state.retry_count += 1
                        continue

                    log.info("Verifier 开始...")
                    if self._abort_repair_if_cancelled(state):
                        cancelled = True
                        break
                    if phase_clock is not None:
                        phase_clock.ensure("verify")
                    t0 = time.time()
                    state.verification_result = self._run_verifier(state)
                    if self._abort_repair_if_cancelled(state):
                        cancelled = True
                        break
                    ms = int((time.time() - t0) * 1000)
                    if phase_clock is not None:
                        phase_clock.consume("verify", ms)
                    self._record_l2_synthetic_ask(
                        state,
                        agent_name="verifier",
                        phase="verify",
                        attempt=state.retry_count,
                        elapsed_ms=ms,
                        stop_reason="verify_done",
                    )
                    set_phase_ms(state.node_timings, "verify", ms)
                    log.info("Verifier 完成: %dms", ms)

                    if state.verification_result.all_passed:
                        state.status = RepairTerminalStatus.FIXED
                        break

                    _record_pytest_exit(state, self._repo_root, "post_patch_pytest_code")

                    if repo_snapshot is not None:
                        self._restore_repo_snapshot(repo_snapshot)
                    else:
                        self._revert_changes(state)
                    state.feedback = self._build_feedback(state.verification_result)
                    self._write_feedback_to_blackboard(state.feedback)
                    state.retry_count += 1

                if (
                    not cancelled
                    and not self._is_repair_cancelled()
                    and should_degrade_to_baseline(
                        state,
                        verification_enabled=self._verification_enabled(),
                        cancelled=cancelled,
                    )
                ):
                    run_baseline_fallback(self, state, initial_snapshot=initial_snapshot)
        except PhaseTimeoutError as exc:
            self._apply_phase_timeout(state, initial_snapshot, exc)
            phase_timed_out = True
        finally:
            if not phase_timed_out and (cancelled or self._is_repair_cancelled()):
                state.node_timings["user_cancel"] = True
                self._restore_repo_snapshot(initial_snapshot)
                self._emit_repair_cancelled(state)

        finalize_repair_state(state)

        total_ms = int((time.time() - t_start) * 1000)
        set_repair_total_ms(state.node_timings, total_ms)
        finalize_phases(state.node_timings)
        self._attach_token_usage(state)
        self._attach_rejection_stats(state)
        self._end_repair_trace(state)
        log.info("总耗时: %dms, status=%s", total_ms, state.status)
        return state
