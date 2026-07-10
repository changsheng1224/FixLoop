"""修复流水线 Template Method（从 Orchestrator 提取）。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.eval.runner import run_pytest
from src.repair.termination import (
    RepairTerminalStatus,
    finalize_repair_state,
    mark_fixed_skip_verify,
)
from src.repair.output_parsers import parse_retrieved_context, parse_suspect_list
from src.repair.timing_schema import (
    finalize_phases,
    set_parallel_wall_ms,
    set_phase_ms,
    set_repair_total_ms,
)
from src.state import RepairState, RetrievedContext, SuspectLocation

log = get_logger("repair.pipeline")


def _record_pytest_exit(state: RepairState, repo_root: str, key: str) -> None:
    code, _ = run_pytest(Path(repo_root))
    state.node_timings[key] = code


class RepairPipelineMixin:
    """Orchestrator 修复主循环与 Localizer/Retriever 步骤。"""

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

    def _repair_impl(self, state: RepairState) -> RepairState:
        """修复流水线主体（可被 repair() 超时包装）。"""
        max_retries = state.max_retries
        issue = state.issue_input

        t_start = time.time()
        self._repair_started_at = t_start
        self._reset_token_tracking()
        self._begin_repair_trace(state)
        log.info("Orchestrator 开始")

        t0 = time.time()
        state.repair_plan = self._parse_issue(issue)
        skill = self._match_skill(issue)
        if skill and state.repair_plan:
            state.repair_plan.estimated_impact = skill.get("suggested_tools", [])
        ms = int((time.time() - t0) * 1000)
        state.node_timings["parse_issue_ms"] = ms
        log.info("parse_issue: %dms", ms)

        log.info("Localizer + Retriever 并行开始...")
        t0 = time.time()
        suspects, context, loc_timing, ret_timing = self._run_localize_and_retrieve(state)
        wall_ms = int((time.time() - t0) * 1000)
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
        state.suspect_locations = suspects
        state.retrieved_context = context
        n = len(suspects)
        n_tests = len(context.related_tests) if context else 0
        log.info(
            "Localizer+Retriever 完成: 墙钟%dms (L=%dms, R=%dms), %d suspect, %d tests",
            wall_ms,
            loc_timing["total_ms"],
            ret_timing["total_ms"],
            n,
            n_tests,
        )

        if self._verification_enabled():
            _record_pytest_exit(state, self._repo_root, "baseline_pytest_code")

        while state.retry_count < max_retries:
            repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
            log.info("Patcher 开始 (retry=%d)...", state.retry_count)
            state.candidate_patches, patch_timing = self._run_patcher(state)
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
                state.retry_count += 1
                continue

            log.info("Verifier 开始...")
            t0 = time.time()
            state.verification_result = self._run_verifier(state)
            ms = int((time.time() - t0) * 1000)
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
            state.retry_count += 1

        finalize_repair_state(state)

        total_ms = int((time.time() - t_start) * 1000)
        set_repair_total_ms(state.node_timings, total_ms)
        finalize_phases(state.node_timings)
        self._attach_token_usage(state)
        self._attach_rejection_stats(state)
        self._end_repair_trace(state)
        log.info("总耗时: %dms, status=%s", total_ms, state.status)
        return state
