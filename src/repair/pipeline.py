"""修复流水线 Template Method（从 Orchestrator 提取）。"""

from __future__ import annotations

import sys as _sys
import time
from concurrent.futures import ThreadPoolExecutor

from src.repair.output_parsers import parse_retrieved_context, parse_suspect_list
from src.state import RepairState, RetrievedContext, SuspectLocation


def _ts() -> str:
    return time.strftime("%H:%M:%S")


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
                print(
                    f"  [localizer] ⚠ 0 suspects, raw[:500]={answer.strip()[:500]!r}",
                    file=_sys.stderr,
                    flush=True,
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
                print(
                    f"  [localizer] 降级: RepairPlan → {len(suspects)} suspect\n",
                    end="",
                    file=_sys.stderr,
                    flush=True,
                )

        return suspects, context, loc_timing, ret_timing

    def _repair_impl(self, state: RepairState) -> RepairState:
        """修复流水线主体（可被 repair() 超时包装）。"""
        max_retries = state.max_retries
        issue = state.issue_input
        timings = {}

        t_start = time.time()
        self._repair_started_at = t_start
        self._reset_token_tracking()
        self._begin_repair_trace(state)
        print(f"[{_ts()}] Orchestrator 开始\n", end="", file=_sys.stderr, flush=True)

        t0 = time.time()
        state.repair_plan = self._parse_issue(issue)
        skill = self._match_skill(issue)
        if skill and state.repair_plan:
            state.repair_plan.estimated_impact = skill.get("suggested_tools", [])
        ms = int((time.time() - t0) * 1000)
        timings["parse_issue_ms"] = ms
        print(f"[{_ts()}] parse_issue: {ms}ms\n", end="", file=_sys.stderr, flush=True)

        print(
            f"[{_ts()}] Localizer + Retriever 并行开始...\n", end="", file=_sys.stderr, flush=True
        )
        t0 = time.time()
        suspects, context, loc_timing, ret_timing = self._run_localize_and_retrieve(state)
        wall_ms = int((time.time() - t0) * 1000)
        timings["localize_retrieve_ms"] = wall_ms
        timings["localizer_ms"] = loc_timing["total_ms"]
        timings["retriever_ms"] = ret_timing["total_ms"]
        state.suspect_locations = suspects
        state.retrieved_context = context
        state.node_timings["localizer_ms"] = loc_timing["total_ms"]
        state.node_timings["localizer_internal"] = loc_timing["internal"]
        state.node_timings["retriever_ms"] = ret_timing["total_ms"]
        state.node_timings["retriever_internal"] = ret_timing["internal"]
        n = len(suspects)
        n_tests = len(context.related_tests) if context else 0
        print(
            f"[{_ts()}] Localizer+Retriever 完成: 墙钟{wall_ms}ms "
            f"(L={loc_timing['total_ms']}ms, R={ret_timing['total_ms']}ms), "
            f"{n} suspect, {n_tests} tests\n",
            end="",
            file=_sys.stderr,
            flush=True,
        )

        while state.retry_count < max_retries:
            repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
            print(
                f"[{_ts()}] Patcher 开始 (retry={state.retry_count})...\n",
                end="",
                file=_sys.stderr,
                flush=True,
            )
            t0 = time.time()
            state.candidate_patches = self._run_patcher(state)
            ms = int((time.time() - t0) * 1000)
            timings["patcher_ms"] = ms
            n = len(state.candidate_patches)
            print(
                f"[{_ts()}] Patcher 完成: {ms}ms, {n}个补丁\n", end="", file=_sys.stderr, flush=True
            )

            if not self._verification_enabled():
                state.status = "patched" if state.candidate_patches else "failed"
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

            print(f"[{_ts()}] Verifier 开始...\n", end="", file=_sys.stderr, flush=True)
            t0 = time.time()
            state.verification_result = self._run_verifier(state)
            ms = int((time.time() - t0) * 1000)
            timings["verifier_ms"] = ms
            print(f"[{_ts()}] Verifier 完成: {ms}ms\n", end="", file=_sys.stderr, flush=True)

            if state.verification_result.all_passed:
                state.status = "fixed"
                break

            if repo_snapshot is not None:
                self._restore_repo_snapshot(repo_snapshot)
            else:
                self._revert_changes(state)
            state.feedback = self._build_feedback(state.verification_result)
            state.retry_count += 1

        if state.status not in ("fixed", "patched"):
            state.status = "exhausted" if state.retry_count >= max_retries else "failed"

        merged = dict(state.node_timings)
        merged.update(timings)
        state.node_timings = merged
        self._attach_token_usage(state)
        self._end_repair_trace(state)
        total_ms = int((time.time() - t_start) * 1000)
        print(
            f"[{_ts()}] 总耗时: {total_ms}ms, status={state.status}\n",
            end="",
            file=_sys.stderr,
            flush=True,
        )
        return state
