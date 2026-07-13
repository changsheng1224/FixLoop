"""修复流水线 Template Method（从 Orchestrator 提取）。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.eval.runner import run_pytest
from src.repair.blackboard_mixin import BlackboardMixin
from src.repair.degrade import run_baseline_fallback, should_degrade_to_baseline
from src.repair.l2_ask_mixin import L2AskMixin
from src.repair.termination import (
    RepairTerminalStatus,
    finalize_repair_state,
    mark_fixed_skip_verify,
)
from src.repair.output_parsers import parse_retrieved_context, parse_suspect_list
from src.repair.phase_clock import PhaseTimeoutError, RepairPhaseClock
from src.repair.timing_schema import (
    finalize_phases,
    set_parallel_wall_ms,
    set_phase_ms,
    set_repair_total_ms,
)
from src.repair.prompt_router import repair_plan_intent_snapshot
from src.repair.run_context import RepairRunContext
from src.state import RepairState, RetrievedContext, SuspectLocation

log = get_logger("repair.pipeline")


def _record_pytest_exit(state: RepairState, repo_root: str, key: str) -> None:
    code, _ = run_pytest(Path(repo_root))
    state.node_timings[key] = code


class RepairPipelineMixin(L2AskMixin, BlackboardMixin):
    """Orchestrator 修复主循环与 Localizer/Retriever 步骤。"""

    _repair_ctx: RepairRunContext | None

    def _make_phase_clock(self) -> RepairPhaseClock | None:
        config = self._active_repair_ctx().phase_timeout_config
        if config is None or not config.any_enabled():
            return None
        return RepairPhaseClock(config)

    def _apply_phase_timeout(
        self,
        state: RepairState,
        initial_snapshot: dict,
        exc: PhaseTimeoutError,
    ) -> None:
        ctx = self._active_repair_ctx()
        if ctx.cancel_token is not None:
            ctx.cancel_token.cancel("timeout")
        self._restore_repo_snapshot(initial_snapshot)
        state.status = RepairTerminalStatus.TIMEOUT
        state.node_timings["phase_timeout"] = exc.phase
        if ctx.phase_timeout_config is not None:
            state.node_timings["phase_timeout_budgets"] = ctx.phase_timeout_config.budget_dict()
        state.node_timings["phase_timeout_consumed_s"] = exc.consumed_s
        state.agent_errors["orchestrator"] = str(exc)
        if ctx.repair_tracer is not None:
            ctx.repair_tracer.emit(
                "orchestrator",
                "phase_timeout",
                {
                    "phase": exc.phase,
                    "budget_s": exc.budget_s,
                    "consumed_s": exc.consumed_s,
                },
            )
        log.warning("阶段超时: %s", exc)

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
            for retry in range(2):
                answer, timing = self._run_agent(
                    self.localizer,
                    prompt,
                    "localizer",
                    state,
                    l2_phase="localize",
                    l2_attempt=retry,
                )
                suspects = parse_suspect_list(answer)
                if suspects:
                    return suspects, timing
                if retry < 1:
                    log.warning(
                        "[localizer] parse 失败 (retry %d), raw[:200]=%r",
                        retry + 1, answer.strip()[:200],
                    )
                    prompt += "\n\n【重试】上次输出未包含有效 JSON。请严格按 SuspectList JSON 格式输出。"
            log.warning("[localizer] 2 次重试后仍无有效 suspects")
            return [], timing

        fast_retrieve = getattr(self, "_fast_retrieve_enabled", False)
        retrieval_path = "rule" if fast_retrieve else "llm"
        state.node_timings["retrieval_path"] = retrieval_path

        if fast_retrieve:
            suspects, loc_timing = run_localizer()
            context, ret_timing = self._rule_retrieve(suspects, issue)
        else:
            def run_retriever():
                prompt = self._retriever_prompt([], plan=plan, issue=issue)
                for retry in range(2):
                    answer, timing = self._run_agent(
                        self.retriever,
                        prompt,
                        "retriever",
                        state,
                        l2_phase="retrieve",
                        l2_attempt=retry,
                    )
                    ctx = parse_retrieved_context(answer)
                    if ctx.related_tests:
                        return ctx, timing
                    if retry < 1:
                        prompt += "\n\n【重试】上次输出未包含有效 JSON。请严格按 RetrievedContext JSON 格式输出。"
                return ctx, timing

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
        ctx = self._active_repair_ctx()

        t_start = time.time()
        ctx.repair_started_at = t_start
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
                tracer = ctx.repair_tracer
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
                state.phase = "localize"
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
                write_stats = self._write_localize_phase_to_blackboard(state, suspects, context)
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
                    state.phase = "patch"
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
                    state.phase = "verify"
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
                    state.feedback = self._build_feedback(
                        state.verification_result, state=state,
                    )
                    self._write_feedback_to_blackboard(state.feedback)
                    state.retry_count += 1

                if (
                    not cancelled
                    and not self._is_repair_cancelled()
                    and should_degrade_to_baseline(
                        state,
                        verification_enabled=self._verification_enabled(),
                        cancelled=cancelled,
                        allow=ctx.allow_baseline_degrade,
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
        state.phase = "done" if state.status == "fixed" else "failed"

        total_ms = int((time.time() - t_start) * 1000)
        set_repair_total_ms(state.node_timings, total_ms)
        finalize_phases(state.node_timings)
        self._save_repair_checkpoint(state)
        self._attach_token_usage(state)
        self._attach_rejection_stats(state)
        self._end_repair_trace(state)
        self._push_repair_metrics(state)
        log.info("总耗时: %dms, status=%s", total_ms, state.status)
        return state

    def _rule_retrieve(
        self, suspects: list, issue: str
    ) -> tuple["RetrievedContext", dict]:
        """规则检索（不调 LLM）：从 suspects 提取函数名 → grep 搜索 → 构建 RetrievedContext。"""
        import re
        import time
        from pathlib import Path

        from agent_runtime.tool_context import ToolContext
        from agent_runtime.tools import tool_grep
        from src.state import RetrievedContext

        t0 = time.time()
        related_tests: list[str] = []
        similar_snippets: list[dict] = []

        # 从 suspects 提取搜索关键词
        keywords: set[str] = set()
        for s in suspects:
            if getattr(s, "function_name", None):
                keywords.add(s.function_name)
            if getattr(s, "class_name", None):
                keywords.add(s.class_name)
        # 从 issue 中提取函数名
        for match in re.finditer(r"def\s+(\w+)|'(\w+)'|\"(\w+)\"", issue):
            for g in match.groups():
                if g and len(g) > 2:
                    keywords.add(g)

        ctx = ToolContext(root=self._repo_root)
        for kw in list(keywords)[:5]:  # 最多 5 个关键词
            grep_out = tool_grep(ctx, {"pattern": kw, "path": ".", "glob": "*.py", "max_results": 10})
            if grep_out and not grep_out.startswith("Error") and grep_out != "(无匹配)":
                for line in grep_out.splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        similar_snippets.append({
                            "file": parts[0],
                            "line": parts[1],
                            "text": parts[2].strip()[:200],
                        })

        # 找对应测试文件
        for s in suspects:
            fname = getattr(s, "file_path", "")
            if fname:
                test_name = f"test_{Path(fname).stem}"
                test_dir = repo / "tests"
                if test_dir.is_dir():
                    for tf in test_dir.rglob("*.py"):
                        if test_name in tf.name:
                            related_tests.append(str(tf.relative_to(repo)))

        elapsed_ms = int((time.time() - t0) * 1000)
        return (
            RetrievedContext(
                similar_snippets=similar_snippets,
                related_tests=related_tests,
            ),
            {"retriever_ms": elapsed_ms, "retrieval_path": "rule"},
        )

    def _save_repair_checkpoint(self, state) -> None:
        """写入 L2 repair_state.json + checkpoint.json（供 --resume-repair + 审计）。"""
        try:
            import json
            import time
            from pathlib import Path

            repair_dir = Path(self._repo_root) / ".agent" / "repairs"
            run_id = getattr(state, "repair_run_id", "") or ""
            sub_dir = repair_dir / run_id if run_id else repair_dir
            sub_dir.mkdir(parents=True, exist_ok=True)

            # 完整 state（含 timings）
            state_path = sub_dir / "repair_state.json"
            state_payload = state.to_dict()
            state_payload["saved_at"] = time.time()
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(state_path)

            # 轻量 checkpoint
            cp_path = sub_dir / "checkpoint.json"
            cp_payload = {
                "status": state.status,
                "phase": state.phase,
                "retry_count": state.retry_count,
                "suspect_count": len(state.suspect_locations),
                "patch_count": len(state.candidate_patches),
                "timings": state.node_timings.get("phases", {}),
                "blackboard": {
                    "entries": sum(1 for _ in state.blackboard_snapshot.get("entries", {}).keys()),
                    "conflicts": state.blackboard_snapshot.get("conflicts", []),
                },
                "saved_at": time.time(),
            }
            tmp2 = cp_path.with_suffix(".tmp")
            tmp2.write_text(json.dumps(cp_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp2.replace(cp_path)
        except Exception:
            pass

    def _push_repair_metrics(self, state) -> None:
        """推送 repair 指标到 Prometheus registry（静默失败）。"""
        try:
            from agent_runtime.metrics import get_registry

            registry = get_registry()
            registry.counter_inc(
                "fixloop_repair_status",
                labels={"status": state.status or "unknown"},
            )
            registry.gauge_set("fixloop_retry_count", state.retry_count)
            for phase, ms in (state.node_timings.get("phases") or {}).items():
                try:
                    registry.gauge_set(
                        "fixloop_repair_phase_ms",
                        float(ms),
                        labels={"phase": phase.replace("_ms", "")},
                    )
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass
