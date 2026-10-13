from __future__ import annotations

import time

from agent_runtime.logging_setup import get_logger
from src.repair.phase_clock import PhaseTimeoutError, RepairPhaseClock
from src.repair.stop_loss import StopLossTracker
from src.repair.timing_schema import set_phase_ms
from src.repair.verification.termination import (
    RepairTerminalStatus,
    mark_pending_verify,
)
from src.state import RepairState, RetrievedContext, SuspectLocation

log = get_logger("repair.pipeline")


class RepairCoordinator:
    def __init__(self, session):
        self.session = session

    def make_phase_clock(self) -> RepairPhaseClock | None:
        config = self.session.asks.active_repair_ctx().phase_timeout_config
        if config is None or not config.any_enabled():
            return None
        return RepairPhaseClock(config)

    def apply_phase_timeout(
        self, state: RepairState, initial_snapshot: dict, exc: PhaseTimeoutError
    ) -> None:
        ctx = self.session.asks.active_repair_ctx()
        if ctx.cancel_token is not None:
            ctx.cancel_token.cancel("timeout")
        if not self.session.lifecycle.cancel_run_resources(state):
            state.control.phase_timeout = exc.phase
            state.agent_errors["orchestrator"] = str(exc)
            return
        keep_patches = bool(state.candidate_patches)
        if not keep_patches:
            salvaged = self.session.transaction.salvage_patches_from_disk(state, initial_snapshot)
            if salvaged:
                state.candidate_patches = salvaged
                state.node_timings["phase_timeout_salvaged"] = len(salvaged)
                keep_patches = True
        if not keep_patches:
            self.session.transaction.restore_repo_snapshot(initial_snapshot)
        else:
            state.node_timings["phase_timeout_kept_patches"] = True
        state.set_status(RepairTerminalStatus.TIMEOUT, "phase_timeout")
        state.control.phase_timeout = exc.phase
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

    def run_critic_gate(self, state: RepairState) -> bool:
        """提交 Verifier 前 Critic。返回 True 表示 reject（应 retry / 停）。"""
        from src.collaboration.isolation import role_projection
        from src.repair.critic import review_patch

        mode = self.session.critic_mode
        allowed = set()
        lock = getattr(self.session, "edit_lock", None)
        if lock is not None:
            allowed = set(lock.allowed_edit)
        elif state.control.allowed_edit:
            allowed = set(state.control.allowed_edit or [])
        self.session.events.emit_repair_span("critic_started", {"summary": f"mode={mode}"})
        state.node_timings["critic_input_projection"] = role_projection(state, "critic")
        verdict = review_patch(state.candidate_patches, allowed_edit=allowed, mode=mode)
        state.node_timings["critic_mode"] = verdict.mode
        state.node_timings["critic_reason"] = verdict.reason
        state.node_timings["critic_verdict"] = verdict.to_dict()
        if verdict.skipped:
            state.node_timings["critic_skipped"] = True
            self.session.events.emit_repair_span(
                "critic_finished", {"summary": "skipped", "skipped": True}
            )
            return False
        em = self.session.events.progress_emitter()
        if not verdict.accepted:
            n = int(state.node_timings.get("critic_rejected_count") or 0) + 1
            state.node_timings["critic_rejected_count"] = n
            tip = (
                f"Critic reject ({verdict.reason})：请生成非空、落在 allowed_edit 内、"
                "语法合法的实现文件 diff；用 apply_patch（含原文上下文）。"
            )
            state.feedback = f"{state.feedback}\n{tip}".strip() if state.feedback else tip
            try:
                self.session.evidence.write_feedback_to_blackboard(state.feedback)
            except RuntimeError:
                pass
            em.emit("critic_progress", summary=f"reject:{verdict.reason}")
            self.session.events.emit_repair_span(
                "critic_finished", {"summary": f"reject:{verdict.reason}", "accepted": False}
            )
            log.info("[critic] reject: %s", verdict.reason)
            return True
        em.emit("critic_progress", summary=f"accept:{verdict.reason}")
        self.session.events.emit_repair_span(
            "critic_finished", {"summary": f"accept:{verdict.reason}", "accepted": True}
        )
        return False

    @staticmethod
    def record_patcher_terminal(state: RepairState) -> str:
        """规范空输出归因，避免把明确终态误报成 JSON 解析失败。"""
        from src.repair.execution.patcher_contract import PATCHER_TERMINAL_STATUSES

        terminal = str(state.control.patcher_terminal_status or "")
        explicit = terminal in PATCHER_TERMINAL_STATUSES
        if (
            not state.candidate_patches
            and (not state.agent_errors.get("patcher_apply"))
            and (not explicit)
        ):
            state.control.patcher_parse_failed = True
        elif explicit:
            state.control.reset("patcher_parse_failed")
            state.agent_errors.pop("patcher_parse", None)
        return terminal

    def handle_empty_patch(self, state: RepairState, stop_loss) -> bool:
        """记录空补丁并决定是否重试；True 表示继续下一轮。"""
        from src.repair.stop_loss import apply_stop_loss

        if state.control.coordination_status == "recovery_required":
            state.set_status("recovery_required", "cleanup_or_write_unconfirmed")
            return False
        if state.control.plan_blocked:
            state.set_status(RepairTerminalStatus.FAILED, "plan_runtime_blocked")
            self.session.events.checkpoint_progress(state)
            return False
        terminal = self.record_patcher_terminal(state)
        if terminal in {"cannot_patch", "insufficient_evidence"}:
            state.feedback = "Patcher 已给出明确终态：" + terminal
            self.session.evidence.write_feedback_to_blackboard(state.feedback)
            self.session.events.checkpoint_progress(state)
            return False
        apply_err = state.agent_errors.get("patcher_apply")
        if apply_err:
            state.control.patcher_apply_failed = True
            state.feedback = (
                f"补丁未能写入文件。原因: {apply_err}。请重新读取原文，"
                "上下文与删除行必须逐字匹配；使用 apply_patch 工具应用补丁。"
            )
        elif terminal == "model_output_truncated":
            state.feedback = (
                "上一轮模型输出在执行写工具前被截断。请基于现有证据调用一次 apply_patch；"
                "若证据不足，请用简短终态明确说明。"
            )
        else:
            state.agent_errors.pop("patcher_apply", None)
            state.control.reset("patcher_apply_failed")
            state.feedback = "补丁生成失败；请基于现有证据直接生成并应用补丁。"
        self.session.evidence.write_feedback_to_blackboard(state.feedback)
        sl = stop_loss.record_empty_patch(apply_failed=bool(apply_err))
        state.control.stop_loss_snapshot = stop_loss.snapshot()
        state.retry_count += 1
        if sl.stop:
            apply_stop_loss(state, sl)
            self.session.evidence.write_feedback_to_blackboard(state.feedback)
            self.session.events.checkpoint_progress(state)
            log.warning("[stop_loss] %s", sl.reason)
            return False
        self.session.events.checkpoint_progress(state)
        return True

    def repair_impl(self, state: RepairState, initial_snapshot: dict | None = None) -> RepairState:
        from agent_runtime.cancellation import CancelledError
        from agent_runtime.linux_sandbox.permissions import SandboxApprovalRequired
        from agent_runtime.run_coordination import OwnerConflictError
        from agent_runtime.test_execution import ProcessCleanupUnconfirmedError
        from src.repair.plan_binding import ResumeRecoveryRequiredError
        from src.repair.recovery_outcome import publish_recovery_outcome
        from src.repair.repo_snapshot import RollbackUnconfirmedError

        try:
            return self.repair_impl_with_plan(state, initial_snapshot)
        except SandboxApprovalRequired as exc:
            state.status = "waiting_approval"
            state.node_timings["sandbox_approval_id"] = exc.approval_id
            self.session.events.checkpoint_progress(state)
            return state
        except OwnerConflictError as exc:
            state.set_status("recovery_required", exc.code)
            state.agent_errors["coordination"] = exc.code
            publish_recovery_outcome(
                state,
                {"run_id": state.repair_run_id, "status": exc.run_status or exc.code},
                stage="owner",
                reason_code=exc.code,
                emitter=self.session.events.progress_emitter(),
            )
            return state
        except RollbackUnconfirmedError as exc:
            context = getattr(self.session.patcher, "tool_context", None)
            if context is not None:
                context.execution_uncertain = True
            state.control.coordination_status = "recovery_required"
            state.set_status("recovery_required", "rollback_outcome_unconfirmed")
            state.agent_errors["rollback"] = str(exc)
            publish_recovery_outcome(
                state,
                {"run_id": state.repair_run_id, "status": "recovery_required"},
                stage="rollback",
                reason_code="rollback_outcome_unconfirmed",
                emitter=self.session.events.progress_emitter(),
            )
            self.session.events.end_repair_trace(state)
            return state
        except ProcessCleanupUnconfirmedError as exc:
            self.session.verification.block_unconfirmed_verification(state, str(exc))
            self.session.events.end_repair_trace(state)
            return state
        except CancelledError:
            state.control.user_cancel = True
            state.set_status("user_cancel", "persisted_cancel_completed")
            self.session.events.end_repair_trace(state)
            return state
        except ResumeRecoveryRequiredError as exc:
            state.control.coordination_status = "recovery_required"
            state.agent_errors["plan_runtime"] = str(exc)
            state.set_status("recovery_required", "cleanup_or_write_unconfirmed")
            previous = state.recovery_outcome
            publish_recovery_outcome(
                state,
                {**previous, "run_id": state.repair_run_id, "status": "recovery_required"},
                stage=previous.get("stage", "resources"),
                reason_code=exc.reason,
                plan_report=previous.get("plan")
                if previous.get("effects_verified") is not None
                else None,
                emitter=self.session.events.progress_emitter(),
            )
            self.session.events.end_repair_trace(state)
            return state
        except Exception:
            if initial_snapshot is not None and self.session.lifecycle.cancel_run_resources(state):
                self.session.transaction.restore_repo_snapshot(initial_snapshot)
            raise
        finally:
            binding = getattr(self.session, "plan_binding", None)
            try:
                self.session.lifecycle.close_sandbox_backend_before_binding_close(state)
                if binding is not None:
                    binding.close()
            finally:
                self.session.plan_binding = None
                self.release_repair_resources()

    def release_repair_resources(self) -> None:
        """Release only this orchestrator's edit lock, including exceptional exits."""
        runtime = getattr(self.session, "collaboration_runtime", None)
        try:
            if runtime is not None:
                runtime.close()
        finally:
            lock = self.session.edit_lock
            context = getattr(self.session.patcher, "tool_context", None)
            if context is not None and context.edit_lock is lock:
                context.edit_lock = None
            self.session.edit_lock = None
            if self.session.progress is not None:
                self.session.progress.stop_heartbeat()

    def repair_impl_with_plan(
        self, state: RepairState, initial_snapshot: dict | None = None
    ) -> RepairState:
        """New and resumed runs share startup, attempt policy and finalization."""
        if initial_snapshot is None:
            initial_snapshot = self.session.transaction.snapshot_repo()
        ctx = self.session.asks.active_repair_ctx()
        checkpoint = ctx.resume_checkpoint
        if checkpoint is not None:
            self.restore_state_from_repair_checkpoint(state, checkpoint)
        t_start = time.time()
        ctx.repair_started_at = t_start
        self.session.events.reset_token_tracking()
        self.session.events.begin_repair_trace(state)
        if ctx.worktree_initial_snapshot:
            initial_snapshot = ctx.worktree_initial_snapshot
        self.session.evidence.init_repair_blackboard()
        if checkpoint is not None:
            self.session.events.restore_blackboard_snapshot(state.blackboard_snapshot)
            from src.repair.execution.edit_lock import EditLockState

            lock = EditLockState(
                repo_root=self.session.repo_root, allowed_edit=state.control.allowed_edit
            )
            lock.allowed_create.update(state.control.allowed_create)
            lock.allowed_delete.update(state.control.allowed_delete)
            lock.write_serial = False
            self.session.edit_lock = lock
            if self.session.patcher is not None:
                self.session.patcher.tool_context.edit_lock = lock
            log.info("[resume] 从 %s 恢复，跳过 parse/localize", state.repair_run_id)
        state.node_timings["repair_mode"] = "patcher_primary"
        state.node_timings["repair_config"] = self.session.repair_config.snapshot()
        emitter = self.session.events.progress_emitter()
        emitter.emit("repair_started", summary="mode=patcher_primary")
        if self.session.repair_config.progress_heartbeat:
            emitter.start_heartbeat(
                interval_s=self.session.repair_config.progress_heartbeat_s, summary="repair_alive"
            )
        cancelled = False
        phase_timed_out = False
        phase_clock = self.make_phase_clock()
        try:
            if self.session.lifecycle.abort_repair_if_cancelled(state):
                cancelled = True
            else:
                if checkpoint is None:
                    self.session.exploration.prepare_repair(state)
                cancelled = self.run_repair_attempts(state, initial_snapshot, phase_clock)
        except PhaseTimeoutError as exc:
            self.apply_phase_timeout(state, initial_snapshot, exc)
            phase_timed_out = True
        finally:
            if not phase_timed_out and (cancelled or self.session.lifecycle.is_repair_cancelled()):
                state.control.user_cancel = True
                if self.session.lifecycle.cancel_run_resources(state):
                    self.session.transaction.restore_repo_snapshot(initial_snapshot)
                self.session.lifecycle.emit_repair_cancelled(state)
        return self.session.events.finalize_repair_run(state, t_start)

    def run_repair_attempts(
        self, state: RepairState, initial_snapshot: dict, phase_clock: RepairPhaseClock | None
    ) -> bool:
        """Run shared policy gates; return whether execution was cancelled."""
        stop_loss = getattr(self.session, "stop_loss", None)
        if stop_loss is None:
            stop_loss = StopLossTracker()
            self.session.stop_loss = stop_loss
        while state.retry_count < state.max_retries:
            if self.session.lifecycle.abort_repair_if_cancelled(state):
                return True
            repo_snapshot = initial_snapshot
            log.info("Patcher 开始 (retry=%d)...", state.retry_count)
            self.session.events.progress_emitter().emit(
                "patcher_turn", summary=f"retry={state.retry_count}"
            )
            self.session.coordination.on_collaboration_phase(state, "patch", "patch attempt")
            if phase_clock is not None:
                phase_clock.ensure("patch")
            patch_started = time.time()
            state.candidate_patches, patch_timing = self.session.patcher_execution.run_patcher(
                state
            )
            patch_ms = patch_timing.get("total_ms")
            if patch_ms is None:
                patch_ms = int((time.time() - patch_started) * 1000)
            if phase_clock is not None:
                phase_clock.consume("patch", patch_ms)
            if patch_timing.get("user_cancel") or self.session.lifecycle.abort_repair_if_cancelled(
                state
            ):
                return True
            set_phase_ms(
                state.node_timings,
                "patch",
                patch_ms,
                internal={
                    "model_call_ms": patch_timing.get("model_call_ms", 0),
                    "parse_apply_ms": patch_timing.get("parse_apply_ms", 0),
                },
            )
            log.info("Patcher 完成: %dms, %d个补丁", patch_ms, len(state.candidate_patches))
            if not state.candidate_patches:
                if self.handle_empty_patch(state, stop_loss):
                    continue
                break
            if self.run_critic_gate(state):
                self.session.transaction.restore_repo_snapshot(repo_snapshot)
                state.candidate_patches = []
                state.retry_count += 1
                self.session.events.checkpoint_progress(state)
                continue
            if self.session.transaction.reject_protected_interface_changes(state, initial_snapshot):
                continue
            if not self.session.verification.verification_enabled():
                mark_pending_verify(state)
                break
            log.info("Verifier 开始...")
            self.session.events.progress_emitter().emit(
                "verify_progress", summary=f"start retry={state.retry_count}"
            )
            if self.session.lifecycle.abort_repair_if_cancelled(state):
                return True
            self.session.coordination.on_collaboration_phase(
                state, "verify", "verification attempt"
            )
            if phase_clock is not None:
                phase_clock.ensure("verify")
            t0 = time.time()
            state.verification_result = self.session.verification.run_verifier(state)
            if self.session.lifecycle.abort_repair_if_cancelled(state):
                return True
            ms = int((time.time() - t0) * 1000)
            if phase_clock is not None:
                phase_clock.consume("verify", ms)
            self.session.asks.record_l2_synthetic_ask(
                state,
                agent_name="verifier",
                phase="verify",
                attempt=state.retry_count,
                elapsed_ms=ms,
                stop_reason="verify_done",
            )
            set_phase_ms(state.node_timings, "verify", ms)
            vr = state.verification_result
            self.session.events.progress_emitter().emit(
                "verify_progress",
                summary=(
                    f"done ms={ms} passed={getattr(vr, 'all_passed', None)} "
                    f"failed={getattr(vr, 'failed', None)}"
                ),
            )
            log.info("Verifier 完成: %dms", ms)
            if state.verification_result.execution_status in {
                "unverified",
                "environment_unavailable",
                "timed_out",
                "cancelled",
            }:
                outcome = state.verification_result.execution_status
                if outcome == "cancelled":
                    state.control.user_cancel = True
                    return True
                if outcome == "timed_out":
                    state.set_status(RepairTerminalStatus.TIMEOUT, "verification_timeout")
                else:
                    state.set_status(RepairTerminalStatus.PENDING_VERIFY, "verification_" + outcome)
                break
            if state.verification_result.all_passed:
                if state.verification_result.verification_mode == "static":
                    state.set_status(RepairTerminalStatus.PENDING_VERIFY, "static_check_completed")
                else:
                    state.set_status(RepairTerminalStatus.FIXED, "verification_passed")
                cooldown = getattr(self.session, "verify_cooldown", None)
                if cooldown is not None:
                    cooldown.record_success()
                break
            if self.record_verification_failure(state, initial_snapshot, stop_loss):
                break
        return False

    def record_verification_failure(
        self, state: RepairState, initial_snapshot: dict, stop_loss: StopLossTracker
    ) -> bool:
        """Roll back, record current evidence, then decide whether to stop."""
        self.session.verification.record_validation_exit(state, "post_patch_pytest_code")
        self.session.transaction.restore_repo_snapshot(initial_snapshot)
        from src.repair.failure_ledger import apply_ledger_to_state, record_verify_into_ledger
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
            state, result=state.verification_result, bucket=diag.bucket.value, is_regression=is_reg
        )
        apply_ledger_to_state(state, ledger)
        state.feedback = self.session.patcher_execution.build_feedback(
            state.verification_result, state=state
        )
        self.session.evidence.write_feedback_to_blackboard(state.feedback)
        sl = stop_loss.record_verify_failure(state.verification_result, state.candidate_patches)
        state.control.stop_loss_snapshot = stop_loss.snapshot()
        if sl.reason == "no_progress" and (not sl.stop):
            state.control.no_progress_warning = dict(sl.meta or {})
            state.agent_errors["no_progress"] = sl.hint
            warning = f"[无进展]\n{sl.hint}"
            state.feedback = f"{warning}\n\n{state.feedback}".strip() if state.feedback else warning
            self.session.evidence.write_feedback_to_blackboard(state.feedback)
        state.control.consecutive_env_fails = stop_loss.snapshot().get("env_streak", 0)
        state.retry_count += 1
        if sl.stop:
            apply_stop_loss(state, sl)
            if sl.reason == "env":
                state.control.verify_env_early_stop = True
                state.agent_errors["verify_env"] = sl.hint
            self.session.evidence.write_feedback_to_blackboard(state.feedback)
            self.session.events.checkpoint_progress(state)
            log.warning("[stop_loss] %s", sl.reason)
            return True
        self.session.events.checkpoint_progress(state)
        return False

    def restore_state_from_repair_checkpoint(self, state: RepairState, checkpoint: dict) -> None:
        """从 checkpoint 恢复长程可续跑字段（含 timings / 策略 / 失败面）。"""
        from agent_runtime.session_contract import compare_workspace_manifest, workspace_manifest
        from src.repair.control_state import RepairControl
        from src.state import CandidatePatch, RepairPlan, VerificationResult, validate_state_payload

        checkpoint = validate_state_payload(checkpoint)
        cancelled = state.control.user_cancel
        coordination = state.control.coordination_status
        state.control = RepairControl.model_validate(checkpoint.get("control") or {})
        from src.repair.execution_state import RepairExecutionState

        state.execution = RepairExecutionState.model_validate(checkpoint.get("execution") or {})
        state.control.user_cancel = cancelled
        state.control.coordination_status = coordination
        state.control.repair_timeout = 0
        state.control.phase_timeout = ""
        state.control.resume_workspace_stale = False
        saved_manifest = checkpoint.get("workspace_manifest") or {}
        if saved_manifest:
            repo_root = getattr(self.session, "repo_root", "") or saved_manifest.get("root", "")
            manifest_diff = compare_workspace_manifest(
                saved_manifest,
                workspace_manifest(
                    repo_root, key_files=list((saved_manifest.get("files") or {}).keys())
                ),
            )
            resume_timings = {"resume_workspace_manifest": manifest_diff}
            state.control.resume_workspace_stale = not manifest_diff["exact_match"]
        else:
            resume_timings = {}
        state.node_timings = {**dict(checkpoint.get("node_timings") or {}), **resume_timings}
        state.control.reset("recovery_outcome")
        state.retry_count = checkpoint.get("retry_count", 0)
        state.max_retries = checkpoint.get("max_retries", state.max_retries)
        state.phase = checkpoint.get("phase", "patch")
        state.feedback = checkpoint.get("feedback", "")
        status = checkpoint.get("status", state.status) or state.status
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
        if state.control.resume_workspace_stale:
            state.retrieved_context = None
            state.candidate_patches = []
            state.verification_result = None
            from src.repair.phase_fsm import resume_phase_for_invalidated_workspace

            state.phase = resume_phase_for_invalidated_workspace(state)
