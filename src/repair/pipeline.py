"""修复流水线 Template Method（从 Orchestrator 提取）。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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


def _split_grep_path_line(line: str) -> tuple[str, str, str] | None:
    """Parse ``path:lineno:text`` without breaking Windows drive letters (E8)."""
    import re

    m = re.match(r"^(.+?):(\d+):(.*)$", line)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


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
    from src.repair.output_parsers import _classify_non_object_retrieve

    if not getattr(ctx, "related_tests", None):
        # Prefer specific incomplete codes when JSON parse failed
        code = _classify_non_object_retrieve(answer)
        if code != "non_json_final":
            return code
        if not (answer or "").strip():
            return "empty_response"
        if "{" not in (answer or "") or "}" not in (answer or ""):
            return "invalid_json"
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
        # E14 / P1：超时前尽量从磁盘 salvage 非空 diff，避免回滚成 empty_model_patch
        keep_patches = bool(state.candidate_patches)
        if not keep_patches:
            salvaged = self._salvage_patches_from_disk(state, initial_snapshot)
            if salvaged:
                state.candidate_patches = salvaged
                state.node_timings["phase_timeout_salvaged"] = len(salvaged)
                keep_patches = True
        if not keep_patches:
            self._restore_repo_snapshot(initial_snapshot)
        else:
            state.node_timings["phase_timeout_kept_patches"] = True
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
                    "kept_patches": keep_patches,
                },
            )
        log.warning("阶段超时: %s", exc)

    def _salvage_patches_from_disk(
        self,
        state: RepairState,
        initial_snapshot: dict,
    ) -> list:
        """对比初始快照与当前工作树，回收已落盘但未登记的补丁。"""
        try:
            after = self._snapshot_repo()
        except Exception as e:
            log.warning("[timeout] salvage snapshot failed: %s", e)
            return []
        from src.repair.edit_from_disk import patches_from_snapshot_diff
        from src.repair.failure_tags import check_patch_faithfulness
        from src.repair.path_resolve import is_impl_py_path

        patches = patches_from_snapshot_diff(
            initial_snapshot, after, explanation="timeout_salvage"
        )
        if not patches:
            return []
        # 优先 faithfulness（含 soft_keep）；否则保留少量实现文件
        kept, _ = check_patch_faithfulness(
            patches, state, soft_keep=True, repo_root=str(self._repo_root or "")
        )
        if kept:
            return kept
        impl = [
            p
            for p in patches
            if p.file_path and is_impl_py_path(p.file_path)
        ]
        return impl[:8] or patches[:4]

    def _run_localizer_only(
        self,
        state: RepairState,
    ) -> tuple[list[SuspectLocation], RetrievedContext, dict, dict]:
        plan = state.repair_plan
        issue = state.issue_input
        from src.repair.localize_fastpath import merge_llm_with_rule_first, rule_first_suspects

        related = []
        if state.retrieved_context is not None:
            related = list(state.retrieved_context.related_tests or [])
        fail_nids = list(state.node_timings.get("verify_failed_nodeids") or [])
        rule_suspects = rule_first_suspects(
            issue or "",
            self._repo_root,
            plan,
            fallback_from_plan=self._fallback_suspects_from_plan,
            related_tests=related,
            fail_nodeids=fail_nids,
        )
        prompt = self._localizer_prompt(plan, issue)
        answer, loc_timing = self._run_localizer_complete_once(state, prompt, attempt=0)
        suspects = parse_suspect_list(answer)
        if not suspects:
            suspects = self._fallback_suspects_from_plan(plan, issue)
        suspects = merge_llm_with_rule_first(
            suspects,
            rule_suspects,
            issue=issue or "",
            repo_root=self._repo_root,
            plan=plan,
            related_tests=related,
            fail_nodeids=fail_nids,
        )
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

        from src.repair.localize_fastpath import merge_llm_with_rule_first, rule_first_suspects
        from src.repair.localize_cheap_explore import cheap_explore_suspects
        from src.repair.localize_memory import remember_confirmed_impls
        from src.repair.localize_tiers import decide_patch_gate
        from src.repair.symbol_index import has_grounded_impl_suspect

        related_seed: list[str] = []
        if state.retrieved_context is not None:
            related_seed = list(state.retrieved_context.related_tests or [])
        fail_nids = list(state.node_timings.get("verify_failed_nodeids") or [])
        test_patch = ""
        if self._repair_ctx is not None:
            test_patch = getattr(self._repair_ctx, "verify_test_patch", "") or ""
        if test_patch:
            state.node_timings["verify_test_patch"] = test_patch  # for seed helpers

        rule_suspects = rule_first_suspects(
            issue or "",
            self._repo_root,
            plan,
            fallback_from_plan=self._fallback_suspects_from_plan,
            related_tests=related_seed,
            fail_nodeids=fail_nids,
            test_patch=test_patch,
            state=state,
        )
        if not has_grounded_impl_suspect(rule_suspects, self._repo_root):
            cheap = cheap_explore_suspects(issue or "", self._repo_root)
            if cheap:
                rule_suspects = list(rule_suspects) + list(cheap)
                state.node_timings["localize_cheap_explore"] = {
                    "count": len(cheap),
                    "top": [s.file_path for s in cheap[:3]],
                }
                log.info(
                    "[localize] cheap explore %d hits top=%s",
                    len(cheap),
                    [s.file_path for s in cheap[:3]],
                )

        if rule_suspects:
            state.suspect_locations = list(rule_suspects)
            state.node_timings["localize_rule_first"] = {
                "count": len(rule_suspects),
                "top": [s.file_path for s in rule_suspects[:3]],
                "f2p_seeded": any((s.reason or "").startswith("F2P") for s in rule_suspects),
                "test_patch_seeded": any(
                    (s.reason or "") == "test_patch覆盖" for s in rule_suspects
                ),
            }
            log.info(
                "[localize] rule-first %d suspects top=%s",
                len(rule_suspects),
                [s.file_path for s in rule_suspects[:3]],
            )

        from src.repair.adaptive_budget import advise_budget

        grounded = has_grounded_impl_suspect(rule_suspects, self._repo_root)
        budget_advice = advise_budget(
            state,
            rule_suspects=rule_suspects,
            grounded=grounded,
            related_tests=related_seed,
        )
        state.node_timings["adaptive_budget"] = budget_advice.to_dict()

        def run_localizer():
            prompt = self._localizer_prompt(plan, issue)
            answer, timing = self._run_localizer_complete_once(state, prompt, attempt=0)
            suspects = parse_suspect_list(answer)
            if suspects:
                return suspects, timing
            log.warning("[localizer] complete_once 未返回有效 suspects")
            return [], timing

        # 空锚且已 cheap explore：跳过 LLM，避免再烧 localize 预算
        skip_llm = budget_advice.skip_llm_localize
        if (
            not skip_llm
            and state.node_timings.get("localize_cheap_explore")
            and grounded
        ):
            skip_llm = True
            state.node_timings["localize_skip_llm_after_cheap"] = True

        fast_retrieve = getattr(self, "_fast_retrieve_enabled", False)
        retrieval_path = "rule" if fast_retrieve else "llm"
        state.node_timings["retrieval_path"] = retrieval_path

        if fast_retrieve:
            if skip_llm:
                suspects, loc_timing = [], {
                    "total_ms": 0,
                    "internal": {
                        "mode": "rule_only",
                        "skipped_llm": True,
                        "reason": budget_advice.reason,
                    },
                }
                log.info("[localize] skip LLM enrich (%s)", budget_advice.reason)
            else:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut_loc = pool.submit(run_localizer)
                    try:
                        suspects, loc_timing = fut_loc.result(
                            timeout=budget_advice.localize_enrich_s
                        )
                    except FuturesTimeoutError:
                        fut_loc.cancel()
                        suspects, loc_timing = [], {
                            "total_ms": int(budget_advice.localize_enrich_s * 1000),
                            "internal": {
                                "mode": "llm_enrich_timeout",
                                "timeout_s": budget_advice.localize_enrich_s,
                            },
                        }
                        state.node_timings["localize_llm_enrich_timeout"] = True
                        log.warning(
                            "[localize] LLM enrich timeout after %.1fs; keep rule suspects",
                            budget_advice.localize_enrich_s,
                        )
            context, ret_timing = self._rule_retrieve(suspects or rule_suspects, issue)
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
                fut_ret = pool.submit(run_retriever)
                if skip_llm:
                    suspects, loc_timing = [], {
                        "total_ms": 0,
                        "internal": {
                            "mode": "rule_only",
                            "skipped_llm": True,
                            "reason": budget_advice.reason,
                        },
                    }
                    log.info("[localize] skip LLM enrich (%s)", budget_advice.reason)
                else:
                    fut_loc = pool.submit(run_localizer)
                    try:
                        suspects, loc_timing = fut_loc.result(
                            timeout=budget_advice.localize_enrich_s
                        )
                    except FuturesTimeoutError:
                        fut_loc.cancel()
                        suspects, loc_timing = [], {
                            "total_ms": int(budget_advice.localize_enrich_s * 1000),
                            "internal": {
                                "mode": "llm_enrich_timeout",
                                "timeout_s": budget_advice.localize_enrich_s,
                            },
                        }
                        state.node_timings["localize_llm_enrich_timeout"] = True
                        log.warning(
                            "[localize] LLM enrich timeout after %.1fs; keep rule suspects",
                            budget_advice.localize_enrich_s,
                        )
                context, ret_timing = fut_ret.result()

            # LLM retriever 失败/空结果 → 强制工具探索，再必要时规则补全
            if context is None or not getattr(context, "related_tests", None):
                if isinstance(ret_timing, dict):
                    state.node_timings["retriever_degrade_reason"] = ret_timing.get(
                        "degrade_reason", "empty_related_tests"
                    )
                if not suspects and plan:
                    suspects = self._fallback_suspects_from_plan(plan, issue)
                context, ret_timing = self._recover_retrieval(
                    state, suspects or rule_suspects, issue, plan, prior=context
                )

        if not suspects:
            suspects = self._fallback_suspects_from_plan(plan, issue)
            if suspects:
                log.info(
                    "[localizer] 降级: RepairPlan → %d suspect",
                    len(suspects),
                )

        related = []
        if context is not None:
            related = list(context.related_tests or [])
        fail_nids = list(state.node_timings.get("verify_failed_nodeids") or [])
        before_n = len(suspects or [])
        suspects = merge_llm_with_rule_first(
            suspects,
            rule_suspects,
            issue=issue or "",
            repo_root=self._repo_root,
            plan=plan,
            related_tests=related,
            fail_nodeids=fail_nids,
            max_keep=8,
        )
        # 仍无 grounded → 再 cheap explore 一次
        if not has_grounded_impl_suspect(suspects, self._repo_root):
            cheap2 = cheap_explore_suspects(issue or "", self._repo_root)
            if cheap2:
                suspects = merge_llm_with_rule_first(
                    cheap2,
                    suspects,
                    issue=issue or "",
                    repo_root=self._repo_root,
                    plan=plan,
                    related_tests=related,
                    fail_nodeids=fail_nids,
                    max_keep=8,
                )
                state.node_timings["localize_cheap_explore_post"] = len(cheap2)

        remember_confirmed_impls(state, suspects, repo_root=str(self._repo_root))
        state.node_timings["_repo_root_hint"] = str(self._repo_root)

        gate = decide_patch_gate(suspects, self._repo_root)
        state.node_timings["localize_patch_gate"] = gate.to_dict()
        if gate.force_short_repair:
            state.node_timings["force_short_repair"] = True

        semantic_hits = [
            s.reason
            for s in suspects
            if s.reason in ("测试导入", "语义扩展", "issue 符号", "调用方扩展", "测试导入模块", "grep命中", "test_patch覆盖")
        ]
        state.node_timings["localize_refined"] = {
            "before": before_n,
            "after": len(suspects),
            "top": [s.file_path for s in suspects[:3]],
            "rule_first": len(rule_suspects),
            "semantic_hits": semantic_hits[:8],
        }
        log.info(
            "[localize] refined suspects %d → %d top=%s semantic=%s gate=%s",
            before_n,
            len(suspects),
            [s.file_path for s in suspects[:3]],
            semantic_hits[:3],
            gate.reason,
        )

        from src.repair.explore_evidence import record_explore_quality

        q = record_explore_quality(
            state, suspects, context, repo_root=str(self._repo_root or "")
        )
        if not q["ok"]:
            log.warning(
                "[explore] insufficient anchor: suspects=%s tests=%s snippets=%s",
                q["n_suspects"],
                q["n_tests"],
                q["n_snippets"],
            )

        return suspects, context, loc_timing, ret_timing

    def _force_explore_enabled(self) -> bool:
        import os

        v = (os.environ.get("FIXLOOP_FORCE_EXPLORE") or "1").strip().lower()
        return v not in ("0", "false", "no", "off")

    def _recover_retrieval(
        self,
        state: RepairState,
        suspects: list,
        issue: str,
        plan,
        *,
        prior=None,
    ) -> tuple:
        """LLM 检索失败后：强制工具探索 → 规则补全 → 合并证据。"""
        from src.repair.explore_evidence import merge_retrieved_context

        forced = None
        forced_timing: dict = {}
        if self._force_explore_enabled() and self.retriever is not None:
            forced, forced_timing = self._force_tool_explore(
                state, suspects, issue, plan
            )
            if forced is not None and (
                forced.related_tests or forced.similar_snippets or forced.caller_locations
            ):
                state.node_timings["retrieval_path"] = forced_timing.get(
                    "retrieval_path", "llm→force_explore"
                )
                log.info(
                    "[retriever] force_explore ok: tests=%d snippets=%d",
                    len(forced.related_tests),
                    len(forced.similar_snippets),
                )
                # 仍用规则补全测试/片段，避免只靠模型
                rule_ctx, rule_timing = self._rule_retrieve(suspects, issue)
                merged = merge_retrieved_context(forced, rule_ctx)
                return merged, {
                    **forced_timing,
                    "rule_ms": rule_timing.get("total_ms", 0),
                    "retrieval_path": state.node_timings["retrieval_path"],
                }

        log.info("[retriever] 降级: → 规则检索 (grep/find_test/snippets)")
        rule_ctx, rule_timing = self._rule_retrieve(suspects, issue)
        merged = merge_retrieved_context(forced or prior, rule_ctx)
        path = "llm→force_explore→rule" if forced is not None else "llm→degrade"
        state.node_timings["retrieval_path"] = path
        return merged, {**rule_timing, "retrieval_path": path}

    def _force_tool_explore(
        self,
        state: RepairState,
        suspects: list,
        issue: str,
        plan,
    ) -> tuple:
        """以 suspects 为锚再跑一轮 Retriever Agent loop（强制 grep/read/submit）。"""
        from src.repair.output_parsers import parse_retrieved_context

        prompt = self._retriever_prompt(suspects or [], plan=plan, issue=issue)
        prompt = (
            f"{prompt}\n\n"
            "【强制探索】上一轮检索未提交有效 related_tests。"
            "必须使用 grep/read_file/find_test 探索仓库，"
            "然后调用 submit_retrieved_context（related_tests 非空）。"
            "禁止空提交或只输出散文。"
        )
        try:
            answer, timing = self._run_agent(
                self.retriever,
                prompt,
                "retriever",
                state,
                l2_phase="retrieve_force",
                l2_attempt=0,
            )
        except Exception as e:
            log.warning("[retriever] force_explore 异常: %s", e)
            return None, {
                "total_ms": 0,
                "internal": {},
                "degrade": True,
                "degrade_reason": "force_explore_exception",
                "retrieval_path": "llm→force_explore_fail",
            }
        ctx = parse_retrieved_context(answer or "")
        path = "llm→force_explore"
        if not ctx.related_tests and not ctx.similar_snippets:
            path = "llm→force_explore_empty"
        return ctx, {
            **timing,
            "retrieval_path": path,
            "force_explore": True,
        }

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
                    # 可执行 Skill Router（与策略 YAML Skill 并存；失败不影响主路径）
                    try:
                        from src.skills.router import SkillRouter

                        decision = SkillRouter().route(issue)
                        tracer.emit("orchestrator", "skill_routed", decision.to_trace_payload())
                    except Exception:
                        pass
                state.node_timings["parse_issue_ms"] = parse_ms
                state.node_timings["skill_resolve_ms"] = skill_ms
                log.info("parse_issue: %dms, skill_resolve: %dms", parse_ms, skill_ms)

                # Skill suggested_tools 仅作 prompt 排序提示。
                # 禁止用跨角色白名单 restrict_to：会 revoke retriever 的
                # read_file/grep/inspect_file（E3 gateway role_not_allowed）。

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
                    # 规则优先：LLM 超时前已有可编辑嫌疑
                    from src.repair.localize_fastpath import seed_rule_first_suspects

                    seed_rule_first_suspects(
                        state,
                        self._repo_root,
                        fallback_from_plan=self._fallback_suspects_from_plan,
                        test_patch=(
                            getattr(self._repair_ctx, "verify_test_patch", "") or ""
                            if self._repair_ctx is not None
                            else ""
                        ),
                    )
                    t0 = time.time()
                    with phase_lock.read():
                        suspects, context, loc_timing, ret_timing = (
                            self._run_localize_and_retrieve(state)
                        )
                    wall_ms = int((time.time() - t0) * 1000)
                    if phase_clock is not None:
                        try:
                            phase_clock.consume("localize", wall_ms)
                        except PhaseTimeoutError as exc:
                            # 已有 grounded suspects 则 soft overrun，进入 patch
                            kept = suspects or state.suspect_locations
                            if kept:
                                state.node_timings["localize_soft_timeout"] = True
                                state.node_timings["localize_soft_timeout_s"] = (
                                    exc.consumed_s
                                )
                                log.warning(
                                    "localize 超预算但仍保留 %d suspects: %s",
                                    len(kept),
                                    exc,
                                )
                            else:
                                raise
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
                    # 定位上限：符号索引再抬一轮；分层门禁决定是否允许 patch
                    from src.repair.localize_quality import (
                        ensure_grounded_suspects,
                        has_grounded_impl_suspect,
                    )
                    from src.repair.localize_tiers import decide_patch_gate

                    related = list(context.related_tests or []) if context else []
                    fail_nids = list(state.node_timings.get("verify_failed_nodeids") or [])
                    suspects, boosted = ensure_grounded_suspects(
                        suspects,
                        repo_root=self._repo_root,
                        issue=state.issue_input or "",
                        plan=state.repair_plan,
                        related_tests=related,
                        fail_nodeids=fail_nids,
                    )
                    if boosted:
                        state.suspect_locations = list(suspects)
                        state.node_timings["localize_index_boost"] = {
                            "after": len(suspects),
                            "top": [s.file_path for s in suspects[:3]],
                        }
                        self._write_localize_phase_to_blackboard(
                            state, suspects, context
                        )
                        log.info(
                            "[localize] symbol-index boost → %d grounded suspects",
                            len(suspects),
                        )
                    gate = decide_patch_gate(suspects, self._repo_root)
                    state.node_timings["localize_patch_gate"] = gate.to_dict()
                    if gate.force_short_repair:
                        state.node_timings["force_short_repair"] = True
                    if not gate.allow:
                        state.agent_errors["localize_ungrounded"] = (
                            f"patch gate blocked: {gate.reason}"
                        )
                        state.node_timings["patch_blocked_ungrounded"] = True
                        tip = (
                            "定位未找到可编辑实现文件：已禁止进入 Patcher。"
                            "请先用 F2P/test_patch/grep/符号索引找到真实源文件。"
                        )
                        state.feedback = (
                            f"{state.feedback}\n{tip}".strip()
                            if state.feedback
                            else tip
                        )
                        skip_patch_loop = True
                        if state.status not in (
                            RepairTerminalStatus.FIXED,
                            RepairTerminalStatus.USER_CANCEL,
                        ):
                            state.status = RepairTerminalStatus.FAILED
                        log.warning("[localize] patch blocked: %s", gate.reason)
                    if state.agent_errors.get("explore_insufficient"):
                        tip = (
                            "探索证据不足（无嫌疑文件/相关测试/代码片段）。"
                            "进入 Patcher 前请先 read_file/grep 定位，勿盲改。"
                        )
                        state.feedback = (
                            f"{state.feedback}\n{tip}".strip()
                            if state.feedback
                            else tip
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

                consecutive_env_fails = 0
                stop_loss = getattr(self, "_stop_loss", None)
                if stop_loss is None:
                    from src.repair.stop_loss import StopLossTracker

                    stop_loss = StopLossTracker()
                    self._stop_loss = stop_loss
                from src.repair.info_gain import apply_info_gain, load_info_gain_from_state
                from src.repair.long_horizon import (
                    apply_horizon_to_state,
                    load_horizon_from_state,
                )

                long_horizon = load_horizon_from_state(state)
                apply_horizon_to_state(state, long_horizon)
                self._long_horizon = long_horizon
                info_gain = load_info_gain_from_state(state)
                apply_info_gain(state, info_gain)
                self._info_gain = info_gain
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
                        apply_err = state.agent_errors.get("patcher_apply")
                        if apply_err:
                            state.node_timings["patcher_apply_failed"] = True
                            state.feedback = (
                                "补丁 JSON 解析成功但未能写入文件。"
                                f" 原因: {apply_err}。"
                                "original_lines 必须与预读代码尽量一致（允许缩进/空白差）；"
                                "对照 near= 中的真实文件行修正 pre-image；"
                                "优先使用 diff 字段；路径必须是仓库内已存在文件。"
                            )
                        else:
                            state.agent_errors.pop("patcher_apply", None)
                            state.node_timings.pop("patcher_apply_failed", None)
                            state.feedback = "补丁生成失败（模型返回无法解析的 JSON）。请重新生成。"
                        self._write_feedback_to_blackboard(state.feedback)
                        from src.repair.stop_loss import apply_stop_loss

                        sl = stop_loss.record_empty_patch(apply_failed=bool(apply_err))
                        state.node_timings["stop_loss_snapshot"] = stop_loss.snapshot()
                        state.retry_count += 1
                        if sl.stop:
                            from src.repair.long_horizon import apply_horizon_to_state

                            decision = long_horizon.on_stop_signal(sl.reason)
                            apply_horizon_to_state(state, long_horizon)
                            if decision.action == "shift" and state.retry_count < max_retries:
                                stop_loss = self._apply_strategy_shift(
                                    state, decision, stop_loss=stop_loss
                                )
                                self._checkpoint_progress(state)
                                continue
                            apply_stop_loss(state, sl)
                            self._write_feedback_to_blackboard(state.feedback)
                            self._checkpoint_progress(state)
                            log.warning("[stop_loss] %s", sl.reason)
                            break
                        self._checkpoint_progress(state)
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

                    from src.repair.stop_loss import apply_stop_loss
                    from src.repair.verify_diagnose import enrich_related_tests_from_diagnosis
                    from src.repair.verify_diagnose import diagnose_verification
                    from src.repair.failure_ledger import (
                        apply_ledger_to_state,
                        record_verify_into_ledger,
                        shrink_suspects_for_regression,
                    )
                    from src.repair.termination import introduced_regression

                    diag = diagnose_verification(state.verification_result)
                    enrich_related_tests_from_diagnosis(state, diag)
                    is_reg = introduced_regression(state)
                    ledger = record_verify_into_ledger(
                        state,
                        result=state.verification_result,
                        bucket=diag.bucket.value,
                        is_regression=is_reg,
                    )
                    if is_reg:
                        state.suspect_locations = shrink_suspects_for_regression(
                            state.suspect_locations, ledger
                        )
                        state.node_timings["regression_scope_shrunk"] = True
                    apply_ledger_to_state(state, ledger)

                    state.feedback = self._build_feedback(
                        state.verification_result,
                        state=state,
                    )
                    if is_reg:
                        tip = (
                            "\n[回归缩 scope] 上轮补丁引入回归；已否定相关文件，"
                            "下一轮优先其它嫌疑，勿继续改回归源文件。"
                        )
                        state.feedback = (state.feedback or "") + tip
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

                    # 长程止损：无进展 / 相同补丁 / 相同验证 / env → 早停
                    gained = info_gain.record(
                        state.verification_result,
                        state.candidate_patches,
                    )
                    apply_info_gain(state, info_gain)
                    if (not gained) and info_gain.should_force_shift() and diag.bucket.value != "env":
                        # 零增益：提前请求长程换策略（等同 no_progress 信号）
                        from src.repair.long_horizon import apply_horizon_to_state as _ah

                        decision = long_horizon.on_stop_signal("no_progress")
                        _ah(state, long_horizon)
                        if decision.action == "shift" and state.retry_count + 1 < max_retries:
                            state.retry_count += 1
                            stop_loss = self._apply_strategy_shift(
                                state, decision, stop_loss=stop_loss
                            )
                            tip = (
                                f"\n[信息增益] 连续 {info_gain.zero_gain_streak} 轮"
                                "无新失败面/无新文件，强制换策略。"
                            )
                            state.feedback = (state.feedback or "") + tip
                            self._write_feedback_to_blackboard(state.feedback)
                            self._checkpoint_progress(state)
                            log.warning(
                                "[info_gain] force shift after %d zero-gain rounds",
                                info_gain.zero_gain_streak,
                            )
                            info_gain.zero_gain_streak = 0
                            apply_info_gain(state, info_gain)
                            continue
                    sl = stop_loss.record_verify_failure(
                        state.verification_result,
                        state.candidate_patches,
                    )
                    state.node_timings["stop_loss_snapshot"] = stop_loss.snapshot()
                    if diag.bucket.value == "env":
                        consecutive_env_fails += 1
                    else:
                        consecutive_env_fails = 0
                    state.node_timings["consecutive_env_fails"] = consecutive_env_fails
                    state.retry_count += 1
                    if sl.stop:
                        from src.repair.long_horizon import apply_horizon_to_state

                        decision = long_horizon.on_stop_signal(sl.reason)
                        apply_horizon_to_state(state, long_horizon)
                        if decision.action == "shift" and state.retry_count < max_retries:
                            stop_loss = self._apply_strategy_shift(
                                state, decision, stop_loss=stop_loss
                            )
                            self._checkpoint_progress(state)
                            log.warning(
                                "[long_horizon] recovered from %s via %s",
                                sl.reason,
                                decision.phase.value,
                            )
                            continue
                        apply_stop_loss(state, sl)
                        if sl.reason == "env":
                            state.node_timings["verify_env_early_stop"] = True
                            state.agent_errors["verify_env"] = sl.hint
                        self._write_feedback_to_blackboard(state.feedback)
                        self._checkpoint_progress(state)
                        log.warning("[stop_loss] %s", sl.reason)
                        break
                    self._checkpoint_progress(state)

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
        from src.repair.long_horizon import (
            apply_horizon_to_state,
            load_horizon_from_state,
        )
        from src.repair.stop_loss import StopLossTracker, apply_stop_loss

        stop_loss = getattr(self, "_stop_loss", None)
        if stop_loss is None:
            stop_loss = StopLossTracker()
            self._stop_loss = stop_loss
        long_horizon = load_horizon_from_state(state)
        apply_horizon_to_state(state, long_horizon)
        self._long_horizon = long_horizon
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
                    sl = stop_loss.record_empty_patch(apply_failed=False)
                    state.node_timings["stop_loss_snapshot"] = stop_loss.snapshot()
                    state.retry_count += 1
                    if sl.stop:
                        decision = long_horizon.on_stop_signal(sl.reason)
                        apply_horizon_to_state(state, long_horizon)
                        if decision.action == "shift" and state.retry_count < state.max_retries:
                            stop_loss = self._apply_strategy_shift(
                                state, decision, stop_loss=stop_loss
                            )
                            self._checkpoint_progress(state)
                            continue
                        apply_stop_loss(state, sl)
                        self._write_feedback_to_blackboard(state.feedback)
                        self._checkpoint_progress(state)
                        break
                    self._checkpoint_progress(state)
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

                from src.repair.verify_diagnose import (
                    diagnose_verification,
                    enrich_related_tests_from_diagnosis,
                )

                diag = diagnose_verification(state.verification_result)
                enrich_related_tests_from_diagnosis(state, diag)
                from src.repair.failure_ledger import (
                    apply_ledger_to_state,
                    build_ledger_prompt_block,
                    record_verify_into_ledger,
                    shrink_suspects_for_regression,
                )
                from src.repair.termination import introduced_regression

                is_reg = introduced_regression(state)
                ledger = record_verify_into_ledger(
                    state,
                    result=state.verification_result,
                    bucket=diag.bucket.value,
                    is_regression=is_reg,
                )
                if is_reg:
                    state.suspect_locations = shrink_suspects_for_regression(
                        state.suspect_locations, ledger
                    )
                    state.node_timings["regression_scope_shrunk"] = True
                ledger_block = build_ledger_prompt_block(ledger)
                if ledger_block and ledger_block not in (state.feedback or ""):
                    state.feedback = (
                        f"{ledger_block}\n\n{state.feedback}".strip()
                        if state.feedback
                        else ledger_block
                    )
                apply_ledger_to_state(state, ledger)
                self._write_feedback_to_blackboard(state.feedback)
                sl = stop_loss.record_verify_failure(
                    state.verification_result,
                    state.candidate_patches,
                )
                state.node_timings["stop_loss_snapshot"] = stop_loss.snapshot()
                state.node_timings["consecutive_env_fails"] = stop_loss.snapshot().get(
                    "env_streak", 0
                )
                state.retry_count += 1
                if sl.stop:
                    decision = long_horizon.on_stop_signal(sl.reason)
                    apply_horizon_to_state(state, long_horizon)
                    if decision.action == "shift" and state.retry_count < state.max_retries:
                        stop_loss = self._apply_strategy_shift(
                            state, decision, stop_loss=stop_loss
                        )
                        self._checkpoint_progress(state)
                        continue
                    apply_stop_loss(state, sl)
                    if sl.reason == "env":
                        state.node_timings["verify_env_early_stop"] = True
                        state.agent_errors["verify_env"] = sl.hint
                    self._write_feedback_to_blackboard(state.feedback)
                    self._checkpoint_progress(state)
                    break
                self._checkpoint_progress(state)
        finally:
            if cancelled or self._is_repair_cancelled():
                state.node_timings["user_cancel"] = True
                self._restore_repo_snapshot(initial_snapshot)
                self._emit_repair_cancelled(state)

        return self._finalize_repair_run(state, t_start)

    def _restore_state_from_repair_checkpoint(self, state: RepairState, checkpoint: dict) -> None:
        """从 checkpoint 恢复长程可续跑字段（含 timings / 策略 / 失败面）。"""
        from src.state import CandidatePatch, RepairPlan, VerificationResult

        state.retry_count = checkpoint.get("retry_count", 0)
        state.max_retries = checkpoint.get("max_retries", state.max_retries)
        state.phase = checkpoint.get("phase", "patch")
        state.feedback = checkpoint.get("feedback", "")
        status = checkpoint.get("status", state.status) or state.status
        # 失败终态续跑回 pending；成功保持 fixed 由上层短路
        if status in ("exhausted", "failed", "timeout", "user_cancel"):
            state.status = "pending"
        else:
            state.status = status
        state.failure_tags = list(checkpoint.get("failure_tags") or [])
        state.node_timings = dict(checkpoint.get("node_timings") or {})
        state.agent_errors = dict(checkpoint.get("agent_errors") or {})
        state.blackboard_snapshot = checkpoint.get("blackboard_snapshot", {}) or {}
        state.degraded_mode = bool(checkpoint.get("degraded_mode", False))
        state.retrieved_context = (
            RetrievedContext.from_dict(checkpoint["retrieved_context"])
            if checkpoint.get("retrieved_context")
            else None
        )
        state.suspect_locations = [
            SuspectLocation.from_dict(s) for s in checkpoint.get("suspect_locations", [])
        ]
        state.candidate_patches = [
            CandidatePatch.from_dict(p) for p in checkpoint.get("candidate_patches", [])
        ]
        if checkpoint.get("verification_result"):
            try:
                state.verification_result = VerificationResult.from_dict(
                    checkpoint["verification_result"]
                )
            except Exception:
                state.verification_result = None
        if checkpoint.get("repair_plan"):
            plan_data = checkpoint["repair_plan"]
            if isinstance(plan_data, dict):
                state.repair_plan = RepairPlan.from_dict(plan_data)
        from src.repair.long_horizon import clear_soft_stop_flags

        clear_soft_stop_flags(state)

    def _checkpoint_progress(self, state: RepairState) -> None:
        """patch/verify 回合中落盘，支持中断后续跑。"""
        try:
            self._save_repair_checkpoint(state)
        except Exception:
            pass
        if state.repair_run_id:
            try:
                from src.repair.checkpoint_load import save_repair_checkpoint

                save_repair_checkpoint(state, self._repo_root)
            except Exception:
                pass

    def _apply_strategy_shift(self, state: RepairState, decision, *, stop_loss):
        """执行扩搜/换假设：刷新嫌疑与检索，重置软止损，写入反馈。"""
        from src.repair.explore_evidence import merge_retrieved_context, record_explore_quality
        from src.repair.localize_quality import refine_suspects, suspects_from_issue
        from src.repair.long_horizon import (
            StrategyPhase,
            apply_horizon_to_state,
            clear_soft_stop_flags,
            load_horizon_from_state,
            reset_stop_loss_tracker,
            strategy_feedback,
        )

        clear_soft_stop_flags(state)
        new_tracker = reset_stop_loss_tracker(stop_loss)
        self._stop_loss = new_tracker

        issue = state.issue_input or ""
        plan = state.repair_plan
        grounded = suspects_from_issue(issue, self._repo_root)
        related = list(
            (state.retrieved_context.related_tests if state.retrieved_context else [])
            or []
        )
        fail_nids = list(state.node_timings.get("verify_failed_nodeids") or [])
        refine_kw = {
            "plan": plan,
            "max_keep": 8,
            "related_tests": related,
            "fail_nodeids": fail_nids,
        }
        if decision.phase == StrategyPhase.SWITCH_HYPOTHESIS:
            # 换假设：降权本轮已改文件 + 账本已否定/回归文件
            from src.repair.failure_ledger import load_ledger_from_state

            burned = {
                str(getattr(p, "file_path", "") or "").replace("\\", "/")
                for p in (state.candidate_patches or [])
                if getattr(p, "file_path", None)
            }
            burned |= load_ledger_from_state(state).forbidden_files()
            prior = [
                s
                for s in (state.suspect_locations or [])
                if str(s.file_path or "").replace("\\", "/") not in burned
            ]
            demoted = [
                s
                for s in (state.suspect_locations or [])
                if str(s.file_path or "").replace("\\", "/") in burned
            ]
            merged_suspects = refine_suspects(
                list(grounded) + prior + demoted,
                issue,
                self._repo_root,
                **refine_kw,
            )
            state.node_timings["strategy_burned_files"] = sorted(burned)[:8]
            state.candidate_patches = []
        else:
            merged_suspects = refine_suspects(
                list(state.suspect_locations or []) + list(grounded),
                issue,
                self._repo_root,
                **refine_kw,
            )
        state.suspect_locations = merged_suspects

        try:
            context, ret_timing = self._recover_retrieval(
                state,
                state.suspect_locations,
                issue,
                plan,
                prior=state.retrieved_context,
            )
            state.retrieved_context = context
            if isinstance(ret_timing, dict):
                state.node_timings["strategy_retrieve"] = ret_timing.get(
                    "retrieval_path", "strategy"
                )
        except Exception as exc:
            log.warning("[long_horizon] strategy retrieve failed: %s", exc)
            try:
                rule_ctx, _ = self._rule_retrieve(state.suspect_locations, issue)
                state.retrieved_context = merge_retrieved_context(
                    state.retrieved_context, rule_ctx
                )
            except Exception:
                pass

        record_explore_quality(
            state,
            state.suspect_locations,
            state.retrieved_context,
            repo_root=str(self._repo_root or ""),
        )
        try:
            self._write_localize_phase_to_blackboard(
                state, state.suspect_locations, state.retrieved_context
            )
        except Exception:
            pass

        tip = strategy_feedback(decision)
        state.feedback = f"{tip}\n\n{state.feedback}".strip() if state.feedback else tip
        self._write_feedback_to_blackboard(state.feedback)

        ctrl = load_horizon_from_state(state)
        ctrl.mark_reconverge()
        apply_horizon_to_state(state, ctrl)
        state.phase = "patch"
        state.node_timings["strategy_last_shift"] = {
            "phase": decision.phase.value,
            "reason": decision.reason,
        }
        log.info(
            "[long_horizon] shift → %s (reason=%s) suspects=%d",
            decision.phase.value,
            decision.reason,
            len(state.suspect_locations),
        )
        return new_tracker

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

        # E17: 优先采用 issue 内 FAIL_TO_PASS 提示并规范为 pytest target
        try:
            from src.benchmark.swebench.convert import (
                extract_fail_to_pass_hints,
                normalize_related_test_refs,
            )

            for hint in normalize_related_test_refs(
                extract_fail_to_pass_hints(issue), self._repo_root
            ):
                if hint and hint not in related_tests:
                    related_tests.append(hint)
        except Exception:
            pass

        # 从 suspects / 栈提取搜索关键词（抑制 issue 引号噪声）
        from src.repair.localize_quality import retrieve_keywords

        keywords = retrieve_keywords(suspects, issue, max_keywords=8)

        ctx = ToolContext(root=self._repo_root)
        for kw in keywords:  # 已截断
            grep_out = tool_grep(
                ctx, {"pattern": rf"\b{re.escape(kw)}\b", "path": ".", "glob": "*.py", "max_results": 10}
            )
            if grep_out and not grep_out.startswith("Error") and grep_out != "(无匹配)":
                for line in grep_out.splitlines():
                    hit = _split_grep_path_line(line)
                    if hit is None:
                        continue
                    fpath, lineno, text = hit
                    similar_snippets.append(
                        {
                            "file": fpath,
                            "line": lineno,
                            "text": text.strip()[:200],
                        }
                    )

        # 找对应测试文件 + find_test 工具
        from src.tools.find_test import find_test_for_function

        for s in suspects:
            fname = getattr(s, "file_path", "")
            if fname:
                test_name = f"test_{Path(fname).stem}"
                test_dir = Path(self._repo_root) / "tests"
                if test_dir.is_dir():
                    for tf in test_dir.rglob("*.py"):
                        if test_name in tf.name:
                            rel = str(tf.relative_to(Path(self._repo_root)))
                            if rel not in related_tests:
                                related_tests.append(rel)
            func = getattr(s, "function_name", "") or ""
            if func and fname:
                try:
                    out = find_test_for_function(
                        ctx, {"function_name": func, "file_path": fname}
                    )
                except Exception:
                    out = ""
                if out and out.strip().startswith("["):
                    try:
                        import json as _json

                        data = _json.loads(out)
                    except Exception:
                        data = None
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and item.get("test_file"):
                                rel = str(item["test_file"]).replace("\\", "/")
                                if rel not in related_tests:
                                    related_tests.append(rel)

        # 将嫌疑文件真源片段写入 similar_snippets（供 Patcher 锚定）
        for s in suspects[:8]:
            fname = getattr(s, "file_path", "") or ""
            if not fname:
                continue
            path = Path(self._repo_root) / fname
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            start_line = max(1, int(getattr(s, "start_line", 1) or 1))
            end_line = max(start_line, int(getattr(s, "end_line", start_line) or start_line))
            ctx_start = max(0, start_line - 4)
            ctx_end = min(len(lines), end_line + 4)
            text = "\n".join(lines[ctx_start:ctx_end])[:600]
            if text.strip():
                similar_snippets.append(
                    {
                        "file": fname.replace("\\", "/"),
                        "line": start_line,
                        "text": text,
                    }
                )

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
            from agent_runtime.observability.labels import low_cardinality_labels

            registry = get_registry()
            status = state.status or "unknown"
            registry.counter_inc(
                "fixloop_repair_status",
                labels=low_cardinality_labels(status=status),
            )
            registry.gauge_set("fixloop_retry_count", state.retry_count)
            for phase, ms in (state.node_timings.get("phases") or {}).items():
                try:
                    registry.gauge_set(
                        "fixloop_repair_phase_ms",
                        float(ms),
                        labels=low_cardinality_labels(phase=phase.replace("_ms", "")),
                    )
                except (ValueError, TypeError):
                    pass
            # 补齐 Grafana 已引用但此前无生产者的指标（低基数 Label）
            tu = state.node_timings.get("token_usage") or {}
            total_tokens = tu.get("total_tokens", state.node_timings.get("total_tokens"))
            if total_tokens is not None:
                try:
                    n = int(total_tokens)
                    if n > 0:
                        registry.counter_inc(
                            "fixloop_token_usage_total",
                            value=n,
                            labels=low_cardinality_labels(status=status),
                        )
                except (TypeError, ValueError):
                    pass
            cache_rate = tu.get("cache_hit_rate")
            if cache_rate is None:
                cache_rate = (state.node_timings.get("runtime_metrics") or {}).get(
                    "cache_hit_rate"
                )
            if cache_rate is not None:
                try:
                    registry.gauge_set(
                        "fixloop_cache_hit_rate",
                        float(cache_rate),
                        labels=low_cardinality_labels(status=status),
                    )
                except (TypeError, ValueError):
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
