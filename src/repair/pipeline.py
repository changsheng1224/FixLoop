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
from src.repair.output_parsers import parse_retrieved_context, parse_suspect_list
from src.repair.phase_clock import PhaseTimeoutError, RepairPhaseClock
from src.repair.prompt_router import repair_plan_intent_snapshot
from src.repair.run_context import RepairRunContext
from src.repair.termination import (
    RepairTerminalStatus,
    finalize_repair_state,
    mark_fixed_skip_verify,
)
from src.repair.timing_schema import (
    finalize_phases,
    set_parallel_wall_ms,
    set_phase_ms,
    set_repair_total_ms,
)
from src.state import CandidatePatch, RepairState, RetrievedContext, SuspectLocation

log = get_logger("repair.pipeline")

LOCALIZER_COMPLETE_ONCE_SYSTEM = """你是代码定位专家。
你的任务是根据 issue、traceback 和嫌疑文件输出 SuspectList。
不要调用工具，不要输出 tool call、XML、Markdown 或解释文字。
只输出合法 JSON 数组；字段包括 file_path, start_line, end_line, function_name, reason, confidence。
如果无法确定精确行号，使用最可能的文件并将 confidence 降低。"""


def _record_pytest_exit(state: RepairState, repo_root: str, key: str) -> None:
    code, _ = run_pytest(Path(repo_root))
    state.node_timings[key] = code


def _is_fake_client(agent) -> bool:
    """检测 Agent 是否使用 FakeModelClient（测试环境，跳过 Planner 避免输出耗尽）。"""
    if agent is None:
        return False
    client = getattr(agent, "model_client", None)
    if client is None:
        return False
    return "Fake" in type(client).__name__


def _retriever_degrade_reason(answer: str, ctx: RetrievedContext) -> str:
    """Classify why LLM retrieval should fall back to rule retrieval."""
    if not (answer or "").strip():
        return "empty_response"
    if "{" not in answer or "}" not in answer:
        return "invalid_json"
    if not getattr(ctx, "related_tests", None):
        return "empty_related_tests"
    return "unknown"


class RepairPipelineMixin(L2AskMixin, BlackboardMixin):
    """Orchestrator 修复主循环与 Localizer/Retriever 步骤。"""

    _repair_ctx: RepairRunContext | None

    def _prune_agents_for_issue(self, plan) -> None:
        """根据 issue_type 动态裁剪 Agent。

        简单问题类型（import_error/syntax_error）仅需 Localizer + Patcher，
        跳过 Retriever 以节省 token 和 latency。
        """
        from src.repair_factory import AgentProfile

        profile = AgentProfile.for_issue_type(plan.issue_type if plan else "")
        if not profile.with_retriever:
            self.retriever = None
            plan.prompt_variants = plan.prompt_variants or {}
            plan.prompt_variants["agent_pruning"] = "skip_retriever"

    # ── Planner Agent ──

    def _plan_with_llm(self, issue: str) -> dict | None:
        """LLM 单次 JSON complete → RepairPlan dict，失败返回 None。

        只规划不调 tool；失败回落规则 _parse_issue。
        trace: planner_invoked · fallback=rule|llm · plan_rationale。
        """
        import json

        from src.agents.planner import PLANNER_PROMPT

        client = getattr(self, "_light_client", None) or getattr(
            getattr(self, "localizer", None), "model_client", None
        )
        if client is None:
            return None

        ctx = self._active_repair_ctx()
        tracer = ctx.repair_tracer if ctx else None
        try:
            prompt = f"{PLANNER_PROMPT}\n\nIssue:\n{issue[:2000]}"
            raw = client.complete(prompt, max_new_tokens=512)
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(raw[start:end])
                if isinstance(data, dict) and "issue_type" in data:
                    if tracer:
                        tracer.emit(
                            "orchestrator",
                            "planner_invoked",
                            {
                                "fallback": "llm",
                                "plan_rationale": str(data.get("reasoning", ""))[:200],
                            },
                        )
                    return data
        except Exception:
            pass

        if tracer:
            tracer.emit(
                "orchestrator",
                "planner_invoked",
                {
                    "fallback": "rule",
                    "plan_rationale": "",
                },
            )
        return None

    def _apply_planner_result(self, plan_dict: dict, plan) -> None:
        """将 Planner LLM 输出应用到 RepairPlan。"""
        plan.issue_type = plan_dict.get("issue_type", plan.issue_type)
        plan.reasoning = plan_dict.get("reasoning", plan.reasoning)
        if plan_dict.get("suspect_files"):
            plan.suspect_files = list(plan_dict["suspect_files"])
        if plan_dict.get("subtasks"):
            from src.state import RepairSubTask

            plan.subtasks = [RepairSubTask.from_dict(s) for s in plan_dict["subtasks"]]
        plan.intent_parser = "llm"

    # ── composite subtasks 编排 ──

    @staticmethod
    def _generate_subtasks(plan) -> list:
        """规则生成 composite 子任务列表（Planner Agent 后置）。"""
        from src.state import RepairSubTask

        if not plan or plan.issue_type != "composite" or len(plan.suspect_files) < 2:
            return []

        subtasks = []
        for i, f in enumerate(plan.suspect_files):
            sid = f"fix_{Path(f).stem}"
            subtasks.append(
                RepairSubTask(
                    id=sid,
                    goal=f"修复 {f} 中的错误",
                    suspect_files=[f],
                    depends_on=[] if i == 0 else [subtasks[0].id],
                )
            )
        # 至少 2 个
        return subtasks if len(subtasks) >= 2 else []

    def _run_subtask_cycle(self, state, subtask, patches_by_subtask: dict) -> list:
        """执行单个子任务的 localize 子循环。

        缩窄 suspect_files 后定位嫌疑位置。
        后续 patch+verify 由主循环统一执行。
        """
        log.info("[subtask] 开始: %s (%s)", subtask.id, subtask.goal)

        tracer = self._active_repair_ctx().repair_tracer if self._active_repair_ctx() else None
        if tracer:
            tracer.emit(
                "orchestrator",
                "subtask_started",
                {
                    "subtask_id": subtask.id,
                    "goal": subtask.goal,
                    "suspect_files": subtask.suspect_files,
                },
            )

        # 缩窄 suspect_files
        plan = state.repair_plan
        original_files = list(plan.suspect_files)
        plan.suspect_files = list(subtask.suspect_files)

        try:
            suspects, context, loc_timing, _ = self._run_localize_and_retrieve(state)
            state.suspect_locations = suspects
            state.retrieved_context = context
            state.node_timings[f"subtask_{subtask.id}_loc_ms"] = loc_timing.get("total_ms", 0)
        finally:
            plan.suspect_files = original_files

        patches_by_subtask[subtask.id] = state.suspect_locations

        if tracer:
            tracer.emit(
                "orchestrator",
                "subtask_done",
                {
                    "subtask_id": subtask.id,
                    "suspect_count": len(state.suspect_locations),
                },
            )
        log.info("[subtask] 完成: %s, suspects=%d", subtask.id, len(state.suspect_locations))
        return state.suspect_locations

    def _merge_subtask_patches(
        self, patches_by_subtask: dict, subtasks: list
    ) -> list[CandidatePatch]:
        """按 depends_on 拓扑合并各子任务的补丁。"""
        merged: list[CandidatePatch] = []
        for st in subtasks:
            sub_patches = patches_by_subtask.get(st.id, [])
            for p in sub_patches:
                if not isinstance(p, CandidatePatch):
                    continue
                if p not in merged:
                    merged.append(p)
        return merged

    def _merge_subtask_suspects(self, suspects_by_subtask: dict, subtasks: list) -> list:
        """按 subtask 顺序合并定位结果，供主 patch loop 使用。"""
        merged: list[SuspectLocation] = []
        seen: set[tuple[str, int, int, str | None]] = set()
        for st in subtasks:
            for suspect in suspects_by_subtask.get(st.id, []):
                if not isinstance(suspect, SuspectLocation):
                    continue
                key = (
                    suspect.file_path,
                    int(suspect.start_line or 0),
                    int(suspect.end_line or 0),
                    suspect.function_name,
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(suspect)
        return merged

    def _prepare_composite_subtasks(self, state: RepairState) -> bool:
        """运行 composite 子任务定位，并把结果合并回主 RepairState。

        子任务只负责缩小定位范围；补丁生成、验证、重试仍走主循环。
        """
        plan = state.repair_plan
        if not plan or plan.issue_type != "composite":
            return False

        subtasks = list(plan.subtasks) if plan.subtasks else []
        if not subtasks:
            subtasks = self._generate_subtasks(plan)
        if not subtasks:
            return False

        plan.subtasks = subtasks
        suspects_by_subtask: dict[str, list] = {}
        for st in subtasks:
            self._run_subtask_cycle(state, st, suspects_by_subtask)

        merged = self._merge_subtask_suspects(suspects_by_subtask, subtasks)
        state.suspect_locations = merged
        if not merged:
            state.status = RepairTerminalStatus.FAILED
            if "subtask_no_suspects" not in state.failure_tags:
                state.failure_tags.append("subtask_no_suspects")
            log.warning("[composite] %d subtasks -> 0 suspects", len(subtasks))
            return False

        log.info("[composite] %d subtasks -> %d suspects merged", len(subtasks), len(merged))
        return True

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
        answer, loc_timing = self._run_localizer_complete_once(state, prompt, attempt=0)
        suspects = parse_suspect_list(answer)
        if not suspects:
            suspects = self._fallback_suspects_from_plan(plan, issue)
        empty_ctx = RetrievedContext()
        ret_timing = {"total_ms": 0, "internal": {}}
        return suspects, empty_ctx, loc_timing, ret_timing

    def _run_localizer_complete_once(
        self,
        state: RepairState,
        prompt: str,
        *,
        attempt: int,
    ) -> tuple[str, dict]:
        """Localizer uses one structured completion; Retriever keeps the ReAct path."""
        from agent_runtime.log_context import log_context

        if self.localizer is not None and state.repair_plan is not None:
            self._inject_repair_task_summary(self.localizer, state)

        from agent_runtime.context_manager import fit_repair_user_prompt

        prompt, pre_budget_meta = fit_repair_user_prompt(
            self.localizer,
            prompt,
            LOCALIZER_COMPLETE_ONCE_SYSTEM,
        )

        t0 = time.time()
        run_id = getattr(self.localizer, "shared_run_id", None)
        task_id = self._begin_l2_agent_ask(
            state,
            self.localizer,
            agent_name="localizer",
            phase="localize",
            attempt=attempt,
        )
        ctx = getattr(self, "_repair_ctx", None)
        tracer = ctx.repair_tracer if ctx is not None else None
        if tracer:
            tracer.emit("localizer", "complete_once_started", {"attempt": attempt})
            tracer.emit("localizer", "model_request_start", {"step": 1, "attempt": attempt + 1})

        usage_before = {}
        if self.localizer is not None:
            from src.eval.token_usage import get_client_session_usage

            usage_before = get_client_session_usage(self.localizer.model_client)

        try:
            with log_context(run_id=run_id, agent="localizer"):
                answer = self.localizer.complete_once(
                    prompt,
                    system_prompt=LOCALIZER_COMPLETE_ONCE_SYSTEM,
                )
        except Exception as e:
            state.agent_errors["localizer"] = str(e)
            log.warning("[localizer] complete_once 失败: %s", e)
            elapsed_ms = int((time.time() - t0) * 1000)
            if task_id:
                self._finish_l2_agent_ask(
                    state,
                    self.localizer,
                    agent_name="localizer",
                    phase="localize",
                    attempt=attempt,
                    task_id=task_id,
                    elapsed_ms=elapsed_ms,
                    stop_reason="error",
                )
            return "", {"total_ms": elapsed_ms, "internal": {"mode": "complete_once"}}

        elapsed_ms = int((time.time() - t0) * 1000)
        if task_id:
            self._finish_l2_agent_ask(
                state,
                self.localizer,
                agent_name="localizer",
                phase="localize",
                attempt=attempt,
                task_id=task_id,
                elapsed_ms=elapsed_ms,
                stop_reason="complete_once",
            )

        if tracer and self.localizer is not None:
            from agent_runtime.model_timing import (
                build_report_latency_fields,
                collect_client_timings,
                emit_model_timing_events,
            )
            from src.eval.token_usage import diff_client_usage, get_client_session_usage

            timings = collect_client_timings(self.localizer.model_client)
            emit_model_timing_events(
                lambda event, payload: tracer.emit("localizer", event, payload),
                timings,
                default_attempt=attempt + 1,
            )
            usage_after = get_client_session_usage(self.localizer.model_client)
            delta = diff_client_usage(usage_before, usage_after)
            budget_meta = getattr(self.localizer, "_last_budget_meta", {}) or {}
            tracer.write_agent_token(
                "localizer",
                delta,
                extra={
                    "tool_steps": 0,
                    "node_timings": {"model_call_ms": elapsed_ms},
                    "prompt_budget": getattr(self.localizer.config, "prompt_budget", None),
                    "budget_cuts": budget_meta.get("cuts", []),
                    "tokenizer_backend": budget_meta.get("tokenizer_backend"),
                    "tokenizer_fallback": budget_meta.get("tokenizer_fallback"),
                    "tokenizer_id": budget_meta.get("tokenizer_id"),
                    "task_template_source": pre_budget_meta.get("task_template_source"),
                    "task_template_fingerprint": pre_budget_meta.get(
                        "task_template_fingerprint"
                    ),
                    **build_report_latency_fields(timings),
                },
            )
            tracer.emit("localizer", "complete_once_finished", {"token_usage": delta})

        return answer, {"total_ms": elapsed_ms, "internal": {"mode": "complete_once"}}

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
            answer, timing = self._run_localizer_complete_once(state, prompt, attempt=0)
            suspects = parse_suspect_list(answer)
            if suspects:
                return suspects, timing
            log.warning("[localizer] complete_once 未返回有效 suspects")
            return [], timing

        fast_retrieve = getattr(self, "_fast_retrieve_enabled", False)
        retrieval_path = "rule" if fast_retrieve else "llm"
        state.node_timings["retrieval_path"] = retrieval_path

        if fast_retrieve:
            suspects, loc_timing = run_localizer()
            context, ret_timing = self._rule_retrieve(suspects, issue)
            state.node_timings["retrieval_path"] = "rule"
        else:

            def run_retriever():
                prompt = self._retriever_prompt([], plan=plan, issue=issue)
                try:
                    answer, timing = self._run_agent(
                        self.retriever,
                        prompt,
                        "retriever",
                        state,
                        l2_phase="retrieve",
                        l2_attempt=0,
                    )
                except Exception:
                    # LLM 异常 → 降级到规则检索
                    log.warning("[retriever] LLM 调用异常，降级到规则检索")
                    return None, {
                        "total_ms": 0,
                        "internal": {},
                        "degrade": True,
                        "degrade_reason": "llm_exception",
                    }
                ctx = parse_retrieved_context(answer)
                if ctx.related_tests:
                    return ctx, timing
                reason = _retriever_degrade_reason(answer, ctx)
                log.info("[retriever] LLM 输出无效，准备降级: %s", reason)
                return None, {
                    **timing,
                    "degrade": True,
                    "degrade_reason": reason,
                }

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_loc = pool.submit(run_localizer)
                fut_ret = pool.submit(run_retriever)
                suspects, loc_timing = fut_loc.result()
                context, ret_timing = fut_ret.result()

            # LLM retriever 失败/空结果 → 自动降级到规则检索
            if context is None or not getattr(context, "related_tests", None):
                log.info("[retriever] 降级: LLM → 规则检索 (grep)")
                state.node_timings["retrieval_path"] = "llm→degrade"
                if isinstance(ret_timing, dict):
                    state.node_timings["retriever_degrade_reason"] = ret_timing.get(
                        "degrade_reason", "empty_related_tests"
                    )
                context, ret_timing = self._rule_retrieve(suspects, issue)

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
        """修复流水线主体（可被 repair() 超时包装）。

        支持 --resume-repair：若有 resume_run_id 且 checkpoint 有效，
        跳过 parse/localize，从 patch 循环重入。
        """
        if initial_snapshot is None:
            initial_snapshot = self._snapshot_repo()

        # ── L2 resume: 从 checkpoint 恢复，跳过 parse/localize ──
        resume_run_id = state.repair_run_id
        if resume_run_id:
            from src.repair.checkpoint_load import load_repair_checkpoint

            cp = load_repair_checkpoint(self._repo_root, resume_run_id)
            if cp:
                return self._repair_from_checkpoint(state, initial_snapshot, cp, resume_run_id)

        max_retries = state.max_retries
        issue = state.issue_input
        ctx = self._active_repair_ctx()

        t_start = time.time()
        ctx.repair_started_at = t_start
        self._reset_token_tracking()
        self._begin_repair_trace(state)
        self._init_repair_blackboard()
        log.info("Orchestrator 开始")

        # 阶段级读写锁：localize/retrieve 共享读，patcher 独占写
        from src.repair.phase_guard import PhaseReadWriteLock

        phase_lock = PhaseReadWriteLock()

        cancelled = False
        phase_timed_out = False
        phase_clock = self._make_phase_clock()
        try:
            if self._abort_repair_if_cancelled(state):
                cancelled = True
            else:
                t0 = time.time()
                state.repair_plan = self._parse_issue(issue)
                # Planner Agent: LLM 单次 JSON → 覆盖规则解析结果
                # 跳过 FakeClient（测试用）以避免输出序列耗尽
                if not _is_fake_client(getattr(self, "localizer", None)):
                    planner_result = self._plan_with_llm(issue)
                    if planner_result:
                        self._apply_planner_result(planner_result, state.repair_plan)
                # 动态 Agent 裁剪：简单问题类型跳过 Retriever
                self._prune_agents_for_issue(state.repair_plan)
                parse_ms = int((time.time() - t0) * 1000)
                if state.repair_plan and state.repair_plan.language != "python":
                    log.info(
                        "检测到 language=%s（%s），Verifier 将使用语言感知静态验证",
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

                # matched skill → suggested_tools 约束 Gateway
                if matched and matched.suggested_tools:
                    for gw in getattr(self, "_repair_gateways", ()):
                        for role in ("localizer", "retriever", "patcher"):
                            gw.restrict_to(role, matched.suggested_tools)

                # 读取相似修复先例（repair precedent 读写一体）
                if state.repair_plan and state.repair_plan.issue_type:
                    from src.repair.precedent import RepairPrecedentStore

                    store = RepairPrecedentStore(self._repo_root)
                    similar = store.load_similar(
                        state.repair_plan.issue_type,
                        query=issue,
                    )
                    if similar:
                        state.node_timings["similar_fixes"] = similar

                localized_from_subtasks = False
                skip_patch_loop = False

                # ── composite subtasks 路径 ──
                if state.repair_plan and state.repair_plan.issue_type == "composite":
                    localized_from_subtasks = self._prepare_composite_subtasks(state)
                    skip_patch_loop = (
                        not localized_from_subtasks and state.status == RepairTerminalStatus.FAILED
                    )

                if not localized_from_subtasks and not skip_patch_loop:
                    log.info("Localizer + Retriever 并行开始...")
                    if phase_clock is not None:
                        phase_clock.ensure("localize")
                    state.phase = "localize"
                    t0 = time.time()
                    with phase_lock.read():
                        suspects, context, loc_timing, ret_timing = (
                            self._run_localize_and_retrieve(state)
                        )
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
                    write_stats = self._write_localize_phase_to_blackboard(
                        state, suspects, context
                    )
                    n = write_stats.get("suspects_written", len(suspects))
                    n_tests = len(context.related_tests) if context else 0
                    log.info(
                        "Localizer+Retriever 完成: 墙钟%dms (L=%dms, R=%dms), "
                        "%d suspect, %d tests",
                        wall_ms,
                        loc_timing["total_ms"],
                        ret_timing["total_ms"],
                        n,
                        n_tests,
                    )
                elif localized_from_subtasks:
                    self._write_localize_phase_to_blackboard(
                        state,
                        state.suspect_locations,
                        state.retrieved_context,
                    )

                if self._abort_repair_if_cancelled(state):
                    cancelled = True
                elif not skip_patch_loop and self._verification_enabled():
                    _record_pytest_exit(state, self._repo_root, "baseline_pytest_code")

                while not skip_patch_loop and not cancelled and state.retry_count < max_retries:
                    if self._abort_repair_if_cancelled(state):
                        cancelled = True
                        break

                    repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
                    log.info("Patcher 开始 (retry=%d)...", state.retry_count)

                    # 冷却轮：连续相同失败 → 降低 temperature
                    cooldown = getattr(self, "_verify_cooldown", None)
                    saved_temp = None
                    if cooldown is not None and cooldown.cooldown_active:
                        saved_temp = self.patcher.config.temperature
                        self.patcher.config.temperature = cooldown.suggested_temperature
                        log.info(
                            "[cooldown] temperature: %.1f → %.1f",
                            saved_temp,
                            cooldown.suggested_temperature,
                        )

                    state.phase = "patch"
                    if phase_clock is not None:
                        phase_clock.ensure("patch")
                    with phase_lock.write():
                        state.candidate_patches, patch_timing = self._run_patcher(state)

                    if saved_temp is not None:
                        self.patcher.config.temperature = saved_temp
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

                    # ── AST 语义等价检查（V1.5-Bonus9）──
                    # 仅检测函数/类签名变更（语法错误不算 drift）
                    semantic_drift = False
                    for patch in state.candidate_patches:
                        try:
                            from src.tools.ast_parser import check_semantic_equivalence

                            result = check_semantic_equivalence(
                                patch.original_lines,
                                patch.patched_lines,
                            )
                            detail = result.get("detail", "")
                            # 跳过 syntax error（非签名级变更，由 verifier 最终裁决）
                            if result["status"] == "drift" and "syntax" not in detail:
                                state.agent_errors["semantic_drift"] = detail
                                semantic_drift = True
                                tracer = ctx.repair_tracer
                                if tracer:
                                    tracer.emit(
                                        "orchestrator",
                                        "semantic_check",
                                        {
                                            "status": "drift",
                                            "detail": detail,
                                            "file": patch.file_path,
                                        },
                                    )
                                log.warning("[semantic] drift: %s → %s", patch.file_path, detail)
                                break
                        except Exception:
                            pass

                    if semantic_drift:
                        state.feedback = (
                            f"AST 语义漂移检测拒绝补丁: {state.agent_errors['semantic_drift']}。"
                            "补丁不得删除或新增函数/类定义。"
                        )
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
                        cooldown = getattr(self, "_verify_cooldown", None)
                        if cooldown is not None:
                            cooldown.record_success()
                        break

                    _record_pytest_exit(state, self._repo_root, "post_patch_pytest_code")

                    if repo_snapshot is not None:
                        self._restore_repo_snapshot(repo_snapshot)
                    else:
                        self._revert_changes(state)
                    state.feedback = self._build_feedback(
                        state.verification_result,
                        state=state,
                    )
                    self._write_feedback_to_blackboard(state.feedback)

                    # Verify 失败冷却轮：连续相同失败 → 降 temperature + 提示
                    cooldown = getattr(self, "_verify_cooldown", None)
                    if cooldown is None:
                        from src.repair.verify_cooldown import VerifyCooldown

                        cooldown = VerifyCooldown()
                        self._verify_cooldown = cooldown
                    cooldown.record_failure(state.verification_result.failure_logs)
                    if cooldown.cooldown_active:
                        state.feedback += f"\n\n{cooldown.cooldown_hint}"
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

        # 修复成功 → 写入先例（repair precedent 读写一体）
        if state.status == "fixed" and state.repair_plan and state.repair_plan.issue_type:
            try:
                from src.repair.precedent import RepairPrecedentStore

                store = RepairPrecedentStore(self._repo_root)
                summary = _build_precedent_summary(state)
                case_id = getattr(self, "_case_id", "") or ""
                store.upsert(state.repair_plan.issue_type, summary, case_id=case_id)
            except Exception:
                pass

        total_ms = int((time.time() - t_start) * 1000)
        set_repair_total_ms(state.node_timings, total_ms)
        finalize_phases(state.node_timings)
        self._save_repair_checkpoint(state)
        self._attach_token_usage(state)
        self._attach_rejection_stats(state)
        # 保存 L2 checkpoint 供 --resume-repair 续跑
        if state.repair_run_id:
            try:
                from src.repair.checkpoint_load import save_repair_checkpoint

                save_repair_checkpoint(state, self._repo_root)
            except Exception:
                pass
        self._end_repair_trace(state)
        self._push_repair_metrics(state)
        log.info("总耗时: %dms, status=%s", total_ms, state.status)
        return state

    def _repair_from_checkpoint(
        self,
        state: RepairState,
        initial_snapshot: dict,
        checkpoint: dict,
        resume_run_id: str,
    ) -> RepairState:
        """Resume a repair checkpoint from the patch boundary."""
        self._restore_state_from_repair_checkpoint(state, checkpoint)
        t_start = time.time()
        self._reset_token_tracking()
        self._begin_repair_trace(state)
        self._init_repair_blackboard()
        self._restore_blackboard_snapshot(state.blackboard_snapshot)
        log.info("[resume] 从 %s 恢复 repair state，跳过 parse/localize", resume_run_id)

        cancelled = False
        try:
            while not cancelled and state.retry_count < state.max_retries:
                if self._abort_repair_if_cancelled(state):
                    cancelled = True
                    break

                repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
                state.phase = "patch"
                t0 = time.time()
                state.candidate_patches, patch_timing = self._run_patcher(state)
                patch_total_ms = patch_timing.get("total_ms")
                if patch_total_ms is None:
                    patch_total_ms = int((time.time() - t0) * 1000)
                set_phase_ms(
                    state.node_timings,
                    "patch",
                    int(patch_total_ms),
                    internal={
                        "model_call_ms": patch_timing.get("model_call_ms", 0),
                        "parse_apply_ms": patch_timing.get("parse_apply_ms", 0),
                    },
                )

                if patch_timing.get("user_cancel") or self._abort_repair_if_cancelled(state):
                    cancelled = True
                    break

                if not self._verification_enabled():
                    if state.candidate_patches:
                        mark_fixed_skip_verify(state)
                    break

                if not state.candidate_patches:
                    state.feedback = "补丁生成失败（模型返回无法解析的 JSON）。请重新生成。"
                    self._write_feedback_to_blackboard(state.feedback)
                    state.retry_count += 1
                    continue

                state.phase = "verify"
                t0 = time.time()
                state.verification_result = self._run_verifier(state)
                verify_ms = int((time.time() - t0) * 1000)
                self._record_l2_synthetic_ask(
                    state,
                    agent_name="verifier",
                    phase="verify",
                    attempt=state.retry_count,
                    elapsed_ms=verify_ms,
                    stop_reason="verify_done",
                )
                set_phase_ms(state.node_timings, "verify", verify_ms)

                if state.verification_result.all_passed:
                    state.status = RepairTerminalStatus.FIXED
                    break

                _record_pytest_exit(state, self._repo_root, "post_patch_pytest_code")
                if repo_snapshot is not None:
                    self._restore_repo_snapshot(repo_snapshot)
                else:
                    self._revert_changes(state)
                state.feedback = self._build_feedback(state.verification_result, state=state)
                self._write_feedback_to_blackboard(state.feedback)
                state.retry_count += 1
        finally:
            if cancelled or self._is_repair_cancelled():
                state.node_timings["user_cancel"] = True
                self._restore_repo_snapshot(initial_snapshot)
                self._emit_repair_cancelled(state)

        return self._finalize_repair_run(state, t_start)

    def _restore_state_from_repair_checkpoint(self, state: RepairState, checkpoint: dict) -> None:
        state.retry_count = checkpoint.get("retry_count", 0)
        state.phase = checkpoint.get("phase", "patch")
        state.feedback = checkpoint.get("feedback", "")
        state.blackboard_snapshot = checkpoint.get("blackboard_snapshot", {})
        state.retrieved_context = (
            RetrievedContext.from_dict(checkpoint["retrieved_context"])
            if checkpoint.get("retrieved_context")
            else None
        )
        state.suspect_locations = [
            SuspectLocation.from_dict(s) for s in checkpoint.get("suspect_locations", [])
        ]
        if checkpoint.get("repair_plan"):
            plan_data = checkpoint["repair_plan"]
            if isinstance(plan_data, dict):
                from src.state import RepairPlan

                state.repair_plan = RepairPlan.from_dict(plan_data)

    def _restore_blackboard_snapshot(self, snapshot: dict | None) -> None:
        ctx = self._active_repair_ctx()
        if ctx.blackboard is None:
            return
        from src.repair.blackboard_merge import restore_blackboard_from_snapshot

        restore_blackboard_from_snapshot(ctx.blackboard, snapshot)

    def _finalize_repair_run(self, state: RepairState, t_start: float) -> RepairState:
        finalize_repair_state(state)
        state.phase = "done" if state.status == "fixed" else "failed"
        total_ms = int((time.time() - t_start) * 1000)
        set_repair_total_ms(state.node_timings, total_ms)
        finalize_phases(state.node_timings)
        self._save_repair_checkpoint(state)
        self._attach_token_usage(state)
        self._attach_rejection_stats(state)
        if state.repair_run_id:
            try:
                from src.repair.checkpoint_load import save_repair_checkpoint

                save_repair_checkpoint(state, self._repo_root)
            except Exception:
                pass
        self._end_repair_trace(state)
        self._push_repair_metrics(state)
        log.info("总耗时: %dms, status=%s", total_ms, state.status)
        return state

    def _rule_retrieve(self, suspects: list, issue: str) -> tuple[RetrievedContext, dict]:
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
            grep_out = tool_grep(
                ctx, {"pattern": kw, "path": ".", "glob": "*.py", "max_results": 10}
            )
            if grep_out and not grep_out.startswith("Error") and grep_out != "(无匹配)":
                for line in grep_out.splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        similar_snippets.append(
                            {
                                "file": parts[0],
                                "line": parts[1],
                                "text": parts[2].strip()[:200],
                            }
                        )

        # 找对应测试文件
        for s in suspects:
            fname = getattr(s, "file_path", "")
            if fname:
                test_name = f"test_{Path(fname).stem}"
                test_dir = Path(self._repo_root) / "tests"
                if test_dir.is_dir():
                    for tf in test_dir.rglob("*.py"):
                        if test_name in tf.name:
                            related_tests.append(str(tf.relative_to(Path(self._repo_root))))

        elapsed_ms = int((time.time() - t0) * 1000)
        return (
            RetrievedContext(
                similar_snippets=similar_snippets,
                related_tests=related_tests,
            ),
            {
                "total_ms": elapsed_ms,
                "internal": {"retriever_ms": elapsed_ms, "retrieval_path": "rule"},
                "retrieval_path": "rule",
            },
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
            tmp.write_text(
                json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
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


def _build_precedent_summary(state) -> str:
    """从 RepairState 构建先例摘要（≤200 chars）。"""
    parts: list[str] = []
    if state.repair_plan:
        if state.repair_plan.reasoning:
            parts.append(state.repair_plan.reasoning[:80])
        if state.repair_plan.suspect_files:
            parts.append("files:" + ",".join(state.repair_plan.suspect_files[:2]))
    if state.candidate_patches:
        p = state.candidate_patches[0]
        diff_preview = (p.diff or "")[:80].replace("\n", " ")
        if diff_preview:
            parts.append(f"patch:{diff_preview}")
    return " ".join(parts)[:200] or "fix applied"
