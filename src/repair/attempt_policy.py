"""Existing repair decisions and their ordered, owner-only side effects."""

from __future__ import annotations

from agent_runtime.logging_setup import get_logger
from src.repair.stop_loss import StopLossTracker
from src.repair.verification.termination import RepairTerminalStatus
from src.state import RepairState

log = get_logger("repair.pipeline")


class RepairAttemptPolicy:
    def __init__(self, session):
        self.session = session

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
        state.retry_count += 1
        if sl.stop:
            apply_stop_loss(state, sl)
            self.session.evidence.write_feedback_to_blackboard(state.feedback)
            self.session.events.checkpoint_progress(state)
            log.warning("[stop_loss] %s", sl.reason)
            return False
        self.session.events.checkpoint_progress(state)
        return True

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
