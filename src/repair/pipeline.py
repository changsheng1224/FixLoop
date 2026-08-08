"""修复流水线 Template Method（从 Orchestrator 提取）。"""

from __future__ import annotations

import time
from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.eval.runner import run_pytest
from src.repair.blackboard_mixin import BlackboardMixin
from src.repair.l2_ask_mixin import L2AskMixin
from src.repair.phase_clock import PhaseTimeoutError, RepairPhaseClock
from src.repair.prompt_router import repair_plan_intent_snapshot
from src.repair.run_context import RepairRunContext
from src.repair.timing_schema import (
    finalize_phases,
    set_phase_ms,
    set_repair_total_ms,
)
from src.repair.verification.termination import (
    RepairTerminalStatus,
    finalize_repair_state,
    mark_fixed_skip_verify,
)
from src.state import RepairState, RetrievedContext, SuspectLocation

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


class RepairPipelineMixin(L2AskMixin, BlackboardMixin):
    """Patcher-primary repair loop with governed verification feedback."""

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
        from src.repair.execution.edit_from_disk import patches_from_snapshot_diff
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

    def _progress_emitter(self):
        em = getattr(self, "_progress", None)
        if em is not None:
            return em
        from src.repair.progress import progress_emitter_from_env

        em = progress_emitter_from_env()
        self._progress = em
        return em

    def _emit_repair_span(self, name: str, payload: dict | None = None) -> None:
        """Trace span（与 ProgressEmitter 阶段名对齐；primary 无 Loc/Ret）。"""
        try:
            ctx = getattr(self, "_repair_ctx", None)
            tracer = getattr(ctx, "repair_tracer", None) if ctx is not None else None
            if tracer is not None:
                tracer.emit("orchestrator", name, payload or {})
        except Exception:
            pass
        try:
            self._progress_emitter().emit(name, summary=str((payload or {}).get("summary", name)))
        except Exception:
            pass

    def _seed_patcher_primary(
        self, state: RepairState
    ) -> tuple[list[SuspectLocation], RetrievedContext]:
        """规则种子 only：不调用 Localizer/Retriever。"""
        started = time.monotonic()
        from src.repair.execution.edit_lock import EditLockState, set_active_edit_lock
        from src.repair.localization.localize_fastpath import seed_rule_first_suspects
        from src.repair.localization.localize_quality import _is_test_path

        state.node_timings["repair_mode"] = "patcher_primary"
        state.node_timings["localize_skipped"] = True
        state.node_timings["retrieve_skipped"] = True
        from src.repair.phase_fsm import RepairPhaseFSM

        RepairPhaseFSM.from_state(state).apply(state, "seed", "primary localization seed")

        test_patch = ""
        if self._repair_ctx is not None:
            test_patch = getattr(self._repair_ctx, "verify_test_patch", "") or ""

        suspects = seed_rule_first_suspects(
            state,
            self._repo_root,
            fallback_from_plan=self._fallback_suspects_from_plan,
            test_patch=test_patch,
            max_keep=5,
            enable_semantic_expand=False,
        )
        # 空种子：不回退 Loc；允许 Patcher 自搜
        if not suspects:
            state.suspect_locations = []
            state.node_timings["primary_seed_empty"] = True

        context = state.retrieved_context or RetrievedContext()
        state.retrieved_context = context

        from src.repair.execution.lock_reflect import f2p_impl_paths, merge_f2p_paths_first
        from src.repair.localization.fail_to_pass_hints import extract_fail_to_pass_hints

        f2p = extract_fail_to_pass_hints(state.issue_input or "")
        if f2p:
            state.node_timings["f2p_hints"] = list(f2p)

        f2p_impls = f2p_impl_paths(state.issue_input or "", self._repo_root, max_keep=8)
        state.node_timings["f2p_impl_paths"] = list(f2p_impls)
        state.node_timings["f2p_seeded"] = bool(f2p_impls)

        allowed: list[str] = []
        for s in state.suspect_locations or []:
            fp = (s.file_path or "").replace("\\", "/")
            if not fp or _is_test_path(fp):
                continue
            if not fp.endswith(".py"):
                continue
            allowed.append(fp)
        plan = state.repair_plan
        if plan is not None:
            for fp in plan.suspect_files or []:
                if not fp or _is_test_path(fp):
                    continue
                allowed.append(fp.replace("\\", "/"))

        # F2P 置顶进锁；其余嫌疑不按目录硬过滤（交给模型）
        allowed_unique = merge_f2p_paths_first(f2p_impls, allowed, max_keep=8)
        if f2p and not f2p_impls:
            state.node_timings["primary_seed_miss_f2p"] = True

        lock = EditLockState(
            repo_root=self._repo_root,
            allowed_edit=set(),
            max_auto_allow=5,
        )
        lock.seed_and_preread(allowed_unique)
        if not lock.allowed_edit:
            state.node_timings["primary_seed_empty_lock"] = True
        lock.require_expand_before_auto = False
        # primary：允许多 hunk；写串行由模型自行节奏，不做硬拒
        lock.write_serial = False
        self._edit_lock = lock
        set_active_edit_lock(self._repo_root, lock)
        state.node_timings["allowed_edit"] = sorted(lock.allowed_edit)
        state.node_timings["unread_write_reject_count"] = 0
        state.node_timings["apply_path_reject_count"] = 0

        set_phase_ms(
            state.node_timings,
            "context",
            int((time.monotonic() - started) * 1000),
        )
        try:
            self._write_seed_context_to_blackboard(
                state, state.suspect_locations, context
            )
        except RuntimeError:
            pass

        em = self._progress_emitter()
        empty_note = " empty_lock→expand_lock" if not lock.allowed_edit else ""
        miss = " miss_f2p" if state.node_timings.get("primary_seed_miss_f2p") else ""
        seed_payload = {
            "summary": (
                f"allowed_edit={len(lock.allowed_edit)} "
                f"suspects={len(state.suspect_locations or [])}{empty_note}{miss}"
            ),
            "allowed_edit": sorted(lock.allowed_edit),
            "f2p": list(f2p)[:5],
            "f2p_impls": list(f2p_impls)[:5],
        }
        em.emit("seed_ready", **seed_payload)
        self._emit_repair_span(
            "seed_span",
            {
                "summary": seed_payload["summary"],
                "allowed_edit_n": len(lock.allowed_edit),
                "f2p_n": len(f2p),
            },
        )
        log.info(
            "[patcher_primary] seed ready allowed_edit=%d suspects=%d (Loc/Ret skipped)",
            len(lock.allowed_edit),
            len(state.suspect_locations or []),
        )
        return list(state.suspect_locations or []), context

    def _run_critic_gate(self, state: RepairState) -> bool:
        """提交 Verifier 前 Critic。返回 True 表示 reject（应 retry / 停）。"""
        from src.repair.critic import resolve_critic_mode, review_patch

        mode = resolve_critic_mode()
        allowed = set()
        lock = getattr(self, "_edit_lock", None)
        if lock is not None:
            allowed = set(lock.allowed_edit)
        elif state.node_timings.get("allowed_edit"):
            allowed = set(state.node_timings.get("allowed_edit") or [])

        self._emit_repair_span("critic_started", {"summary": f"mode={mode}"})
        verdict = review_patch(
            state.candidate_patches,
            allowed_edit=allowed,
            mode=mode,
        )
        state.node_timings["critic_mode"] = verdict.mode
        state.node_timings["critic_reason"] = verdict.reason
        if verdict.skipped:
            state.node_timings["critic_skipped"] = True

        em = self._progress_emitter()
        if not verdict.accepted:
            n = int(state.node_timings.get("critic_rejected_count") or 0) + 1
            state.node_timings["critic_rejected_count"] = n
            tip = (
                f"Critic reject ({verdict.reason})："
                "请生成非空、落在 allowed_edit 内、语法合法的实现文件 diff；"
                "用 apply_patch（含 - 上下文），避免 @@ -1 截断片与重复行。"
            )
            state.feedback = f"{state.feedback}\n{tip}".strip() if state.feedback else tip
            try:
                self._write_feedback_to_blackboard(state.feedback)
            except RuntimeError:
                pass
            em.emit("critic_progress", summary=f"reject:{verdict.reason}")
            self._emit_repair_span(
                "critic_finished",
                {"summary": f"reject:{verdict.reason}", "accepted": False},
            )
            log.info("[critic] reject: %s", verdict.reason)
            return True

        em.emit("critic_progress", summary=f"accept:{verdict.reason}")
        self._emit_repair_span(
            "critic_finished",
            {"summary": f"accept:{verdict.reason}", "accepted": True},
        )
        return False

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
        state.node_timings["repair_mode"] = "patcher_primary"
        em0 = self._progress_emitter()
        em0.emit(
            "repair_started",
            summary=f"mode={state.node_timings['repair_mode']}",
        )
        import os

        if (os.environ.get("FIXLOOP_PROGRESS_HEARTBEAT") or "1").strip().lower() not in (
            "0",
            "false",
            "off",
            "no",
        ):
            try:
                interval = float(os.environ.get("FIXLOOP_PROGRESS_HEARTBEAT_S") or "60")
            except ValueError:
                interval = 60.0
            em0.start_heartbeat(interval_s=max(5.0, interval), summary="repair_alive")

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
                        from src.skills.decision import build_canonical_skill_decision
                        from src.skills.router import SkillRouter

                        decision = SkillRouter().route(issue)
                        tracer.emit("orchestrator", "skill_routed", decision.to_trace_payload())
                        canonical = build_canonical_skill_decision(matched, decision)
                        state.repair_plan.skill.canonical_decision = canonical.to_dict()
                        tracer.emit("orchestrator", "skill_decided", canonical.to_dict())
                        if canonical.fallback:
                            tracer.emit("orchestrator", "skill_fallback", canonical.to_dict())
                    except Exception:
                        pass
                state.node_timings["parse_issue_ms"] = parse_ms
                state.node_timings["skill_resolve_ms"] = skill_ms
                log.info("parse_issue: %dms, skill_resolve: %dms", parse_ms, skill_ms)

                # Skill suggested_tools only reorder prompt hints; ToolSpec owns access.

                # 读取相似修复先例（repair precedent 读写一体）
                if state.repair_plan and state.repair_plan.issue_type:
                    from src.repair.precedent import RepairPrecedentStore

                    store = RepairPrecedentStore(self._repo_root)
                    similar = store.load_similar(
                        state.repair_plan.issue_type,
                        query="",
                        use_semantic=False,
                    )
                    if similar:
                        state.node_timings["similar_fixes"] = similar
                    state.node_timings["precedent_semantic_skipped"] = True

                skip_patch_loop = False
                self._seed_patcher_primary(state)

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
                while not skip_patch_loop and not cancelled and state.retry_count < max_retries:
                    if self._abort_repair_if_cancelled(state):
                        cancelled = True
                        break

                    repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
                    log.info("Patcher 开始 (retry=%d)...", state.retry_count)
                    self._progress_emitter().emit(
                        "patcher_turn",
                        summary=f"retry={state.retry_count}",
                    )

                    # 冷却轮：连续相同失败 → 降低 temperature（pipeline only）
                    self._on_collaboration_phase(state, "patch", "patch attempt")
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
                            apply_stop_loss(state, sl)
                            self._write_feedback_to_blackboard(state.feedback)
                            self._checkpoint_progress(state)
                            log.warning("[stop_loss] %s (primary)", sl.reason)
                            break
                        self._checkpoint_progress(state)
                        continue

                    # Critic：空/越锁/仅测试 → 回灌，不进沙箱
                    if self._run_critic_gate(state):
                        state.candidate_patches = []
                        state.retry_count += 1
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
                    self._progress_emitter().emit(
                        "verify_progress", summary=f"start retry={state.retry_count}"
                    )
                    if self._abort_repair_if_cancelled(state):
                        cancelled = True
                        break
                    self._on_collaboration_phase(state, "verify", "verification attempt")
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
                    vr = state.verification_result
                    self._progress_emitter().emit(
                        "verify_progress",
                        summary=(
                            f"done ms={ms} passed={getattr(vr, 'all_passed', None)} "
                            f"failed={getattr(vr, 'failed', None)}"
                        ),
                    )
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

                    from src.repair.failure_ledger import (
                        apply_ledger_to_state,
                        record_verify_into_ledger,
                    )
                    from src.repair.stop_loss import apply_stop_loss
                    from src.repair.verification.termination import introduced_regression
                    from src.repair.verification.verify_diagnose import (
                        diagnose_verification,
                        enrich_related_tests_from_diagnosis,
                    )

                    diag = diagnose_verification(state.verification_result)
                    enrich_related_tests_from_diagnosis(state, diag)
                    is_reg = introduced_regression(state)
                    ledger = record_verify_into_ledger(
                        state,
                        result=state.verification_result,
                        bucket=diag.bucket.value,
                        is_regression=is_reg,
                    )
                    apply_ledger_to_state(state, ledger)

                    state.feedback = self._build_feedback(
                        state.verification_result,
                        state=state,
                    )
                    self._write_feedback_to_blackboard(state.feedback)
                    sl = stop_loss.record_verify_failure(
                        state.verification_result,
                        state.candidate_patches,
                    )
                    state.node_timings["stop_loss_snapshot"] = stop_loss.snapshot()
                    if sl.reason == "no_progress" and not sl.stop:
                        state.node_timings["no_progress_warning"] = dict(sl.meta or {})
                        state.agent_errors["no_progress"] = sl.hint
                        warning = f"[无进展]\n{sl.hint}"
                        state.feedback = (
                            f"{warning}\n\n{state.feedback}".strip()
                            if state.feedback
                            else warning
                        )
                        self._write_feedback_to_blackboard(state.feedback)
                    if diag.bucket.value == "env":
                        consecutive_env_fails += 1
                    else:
                        consecutive_env_fails = 0
                    state.node_timings["consecutive_env_fails"] = consecutive_env_fails
                    state.retry_count += 1
                    if sl.stop:
                        apply_stop_loss(state, sl)
                        if sl.reason == "env":
                            state.node_timings["verify_env_early_stop"] = True
                            state.agent_errors["verify_env"] = sl.hint
                        self._write_feedback_to_blackboard(state.feedback)
                        self._checkpoint_progress(state)
                        log.warning("[stop_loss] %s", sl.reason)
                        break
                    self._checkpoint_progress(state)

        except PhaseTimeoutError as exc:
            self._apply_phase_timeout(state, initial_snapshot, exc)
            phase_timed_out = True
        finally:
            if not phase_timed_out and (cancelled or self._is_repair_cancelled()):
                state.node_timings["user_cancel"] = True
                self._restore_repo_snapshot(initial_snapshot)
                self._emit_repair_cancelled(state)

        finalize_repair_state(state)
        self._on_collaboration_phase(
            state,
            "done" if state.status == "fixed" else "failed",
            "repair finalized",
        )

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
        lock = getattr(self, "_edit_lock", None)
        if lock is not None:
            state.node_timings["unread_write_reject_count"] = int(
                getattr(lock, "unread_write_reject_count", 0) or 0
            )
            state.node_timings["apply_path_reject_count"] = int(
                getattr(lock, "apply_path_reject_count", 0) or 0
            )
        try:
            from src.repair.execution.edit_lock import clear_active_edit_lock

            clear_active_edit_lock(self._repo_root)
        except Exception:
            pass
        em_done = self._progress_emitter()
        try:
            em_done.stop_heartbeat()
        except Exception:
            pass
        em_done.emit(
            "repair_finished",
            summary=f"status={state.status} total_ms={total_ms}",
        )
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
        from src.repair.stop_loss import StopLossTracker, apply_stop_loss

        stop_loss = getattr(self, "_stop_loss", None)
        if stop_loss is None:
            stop_loss = StopLossTracker()
            self._stop_loss = stop_loss
        try:
            while not cancelled and state.retry_count < state.max_retries:
                if self._abort_repair_if_cancelled(state):
                    cancelled = True
                    break

                repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
                self._on_collaboration_phase(state, "patch", "resume patch attempt")
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
                        apply_stop_loss(state, sl)
                        self._write_feedback_to_blackboard(state.feedback)
                        self._checkpoint_progress(state)
                        break
                    self._checkpoint_progress(state)
                    continue

                self._on_collaboration_phase(state, "verify", "resume verification attempt")
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

                from src.repair.verification.verify_diagnose import (
                    diagnose_verification,
                    enrich_related_tests_from_diagnosis,
                )

                diag = diagnose_verification(state.verification_result)
                enrich_related_tests_from_diagnosis(state, diag)
                from src.repair.failure_ledger import (
                    apply_ledger_to_state,
                    record_verify_into_ledger,
                )
                from src.repair.verification.termination import introduced_regression

                is_reg = introduced_regression(state)
                ledger = record_verify_into_ledger(
                    state,
                    result=state.verification_result,
                    bucket=diag.bucket.value,
                    is_regression=is_reg,
                )
                apply_ledger_to_state(state, ledger)
                self._write_feedback_to_blackboard(state.feedback)
                sl = stop_loss.record_verify_failure(
                    state.verification_result,
                    state.candidate_patches,
                )
                state.node_timings["stop_loss_snapshot"] = stop_loss.snapshot()
                if sl.reason == "no_progress" and not sl.stop:
                    state.node_timings["no_progress_warning"] = dict(sl.meta or {})
                    state.agent_errors["no_progress"] = sl.hint
                    warning = f"[无进展]\n{sl.hint}"
                    state.feedback = (
                        f"{warning}\n\n{state.feedback}".strip()
                        if state.feedback
                        else warning
                    )
                    self._write_feedback_to_blackboard(state.feedback)
                state.node_timings["consecutive_env_fails"] = stop_loss.snapshot().get(
                    "env_streak", 0
                )
                state.retry_count += 1
                if sl.stop:
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
        from agent_runtime.session_contract import compare_workspace_manifest, workspace_manifest
        from src.state import CandidatePatch, RepairPlan, VerificationResult

        saved_manifest = checkpoint.get("workspace_manifest") or {}
        if saved_manifest:
            repo_root = getattr(self, "_repo_root", "") or saved_manifest.get("root", "")
            manifest_diff = compare_workspace_manifest(
                saved_manifest,
                workspace_manifest(
                    repo_root,
                    key_files=list((saved_manifest.get("files") or {}).keys()),
                ),
            )
            state.node_timings["resume_workspace_manifest"] = manifest_diff
            if not manifest_diff["exact_match"]:
                state.node_timings["resume_workspace_stale"] = True

        state.node_timings = dict(checkpoint.get("node_timings") or {})
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
        state.state_revision = int(checkpoint.get("state_revision", state.state_revision) or 0)
        state.attempt = int(checkpoint.get("attempt", state.attempt) or 0) + 1
        state.intent = dict(checkpoint.get("intent") or {})
        state.hypotheses = list(checkpoint.get("hypotheses") or [])
        state.evidence = list(checkpoint.get("evidence") or [])
        state.changed_files = list(checkpoint.get("changed_files") or [])
        state.tool_budget = dict(checkpoint.get("tool_budget") or {})
        state.active_roles = list(checkpoint.get("active_roles") or [])
        state.role_lifecycle = dict(checkpoint.get("role_lifecycle") or {})
        state.blackboard_revision = int(checkpoint.get("blackboard_revision", 0) or 0)
        state.field_owners = dict(checkpoint.get("field_owners") or {})
        state.phase_history = list(checkpoint.get("phase_history") or [])
        state.workspace_manifest = dict(checkpoint.get("workspace_manifest") or {})
        state.action_ledger = list(checkpoint.get("action_ledger") or [])
        state.side_effects = list(checkpoint.get("side_effects") or [])
        state.checkpoint_id = str(checkpoint.get("checkpoint_id", "") or "")
        state.checkpoint_sequence = int(checkpoint.get("checkpoint_sequence", 0) or 0)
        if state.node_timings.get("resume_workspace_stale"):
            state.retrieved_context = None
            state.candidate_patches = []
            state.verification_result = None
            from src.repair.phase_fsm import resume_phase_for_invalidated_workspace

            state.phase = resume_phase_for_invalidated_workspace(state)

    def _checkpoint_progress(self, state: RepairState) -> None:
        """patch/verify 回合中落盘，支持中断后续跑。"""
        decision = state.node_timings.get("repair_failure_decision")
        if isinstance(decision, dict):
            state.node_timings["checkpoint_next_action"] = decision.get("next_action", "")
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

    def _restore_blackboard_snapshot(self, snapshot: dict | None) -> None:
        ctx = self._active_repair_ctx()
        if ctx.blackboard is None:
            return
        from src.repair.blackboard_merge import restore_blackboard_from_snapshot

        restore_blackboard_from_snapshot(ctx.blackboard, snapshot)

    def _finalize_repair_run(self, state: RepairState, t_start: float) -> RepairState:
        finalize_repair_state(state)
        self._on_collaboration_phase(
            state,
            "done" if state.status == "fixed" else "failed",
            "recovery finalized",
        )
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
