"""Owner-only stage services; FixLoop remains the source of business state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_runtime.logging_setup import get_logger
from src.repair.attempt_policy import RepairAttemptPolicy
from src.repair.graph_owner import OwnerCalls
from src.repair.phase_clock import RepairPhaseClock
from src.repair.stop_loss import StopLossTracker
from src.repair.timing_schema import set_phase_ms
from src.repair.verification.termination import RepairTerminalStatus, mark_pending_verify
from src.state import RepairState

if TYPE_CHECKING:
    from src.repair.session import ExecutionSession

log = get_logger("repair.pipeline")


@dataclass
class RepairGraphContext:
    session: ExecutionSession
    state: RepairState
    initial_snapshot: dict
    phase_clock: RepairPhaseClock | None
    resume: bool
    owner: OwnerCalls
    cancelled: bool = False


class RepairStages:
    def __init__(self, context: RepairGraphContext):
        self.context = context
        self.session = context.session
        self.state = context.state
        self.policy = RepairAttemptPolicy(self.session)

    def _cancelled(self) -> bool:
        if self.session.lifecycle.abort_repair_if_cancelled(self.state):
            self.context.cancelled = True
            return True
        return False

    def _stop_loss(self):
        if getattr(self.session, "stop_loss", None) is None:
            self.session.stop_loss = StopLossTracker()
        return self.session.stop_loss

    def entry(self) -> str:
        if self._cancelled():
            return "done"
        return "attempt" if self.context.resume else "prepare"

    def prepare(self) -> str:
        self.session.exploration.prepare_repair(self.state)
        return "attempt"

    def attempt(self) -> str:
        self._stop_loss()
        if self.state.retry_count >= self.state.max_retries or self._cancelled():
            return "done"
        return "patch"

    def patch(self) -> str:
        state = self.state
        phase_clock = self.context.phase_clock
        log.info("Patcher 开始 (retry=%d)...", state.retry_count)
        self.session.events.progress_emitter().emit(
            "patcher_turn", summary=f"retry={state.retry_count}"
        )
        self.session.coordination.on_collaboration_phase(state, "patch", "patch attempt")
        if phase_clock is not None:
            phase_clock.ensure("patch")
        started = time.time()
        state.candidate_patches, timing = self.session.patcher_execution.run_patcher(state)
        elapsed_ms = timing.get("total_ms")
        if elapsed_ms is None:
            elapsed_ms = int((time.time() - started) * 1000)
        if phase_clock is not None:
            phase_clock.consume("patch", elapsed_ms)
        if timing.get("user_cancel"):
            self.context.cancelled = True
            return "done"
        if self._cancelled():
            return "done"
        set_phase_ms(
            state.node_timings,
            "patch",
            elapsed_ms,
            internal={
                "model_call_ms": timing.get("model_call_ms", 0),
                "parse_apply_ms": timing.get("parse_apply_ms", 0),
            },
        )
        log.info("Patcher 完成: %dms, %d个补丁", elapsed_ms, len(state.candidate_patches))
        return "critic" if state.candidate_patches else "empty_patch"

    def empty_patch(self) -> str:
        retry = self.policy.handle_empty_patch(self.state, self._stop_loss())
        return "attempt" if retry else "done"

    def critic(self) -> str:
        if self.policy.run_critic_gate(self.state):
            self.session.transaction.restore_repo_snapshot(self.context.initial_snapshot)
            self.state.candidate_patches = []
            self.state.retry_count += 1
            self.session.events.checkpoint_progress(self.state)
            return "attempt"
        return "protected_interface"

    def protected_interface(self) -> str:
        if self.session.transaction.reject_protected_interface_changes(
            self.state, self.context.initial_snapshot
        ):
            return "attempt"
        return "verification_gate"

    def verification_gate(self) -> str:
        return "verify" if self.session.verification.verification_enabled() else "pending_verify"

    def pending_verify(self) -> str:
        mark_pending_verify(self.state, "verification_disabled")
        return "done"

    def verify(self) -> str:
        state = self.state
        phase_clock = self.context.phase_clock
        log.info("Verifier 开始...")
        self.session.events.progress_emitter().emit(
            "verify_progress", summary=f"start retry={state.retry_count}"
        )
        if self._cancelled():
            return "done"
        self.session.coordination.on_collaboration_phase(state, "verify", "verification attempt")
        if phase_clock is not None:
            phase_clock.ensure("verify")
        started = time.time()
        state.verification_result = self.session.verification.run_verifier(state)
        if self._cancelled():
            return "done"
        elapsed_ms = int((time.time() - started) * 1000)
        if phase_clock is not None:
            phase_clock.consume("verify", elapsed_ms)
        self.session.asks.record_l2_synthetic_ask(
            state,
            agent_name="verifier",
            phase="verify",
            attempt=state.retry_count,
            elapsed_ms=elapsed_ms,
            stop_reason="verify_done",
        )
        set_phase_ms(state.node_timings, "verify", elapsed_ms)
        result = state.verification_result
        self.session.events.progress_emitter().emit(
            "verify_progress",
            summary=f"done ms={elapsed_ms} passed={result.all_passed} failed={result.failed}",
        )
        log.info("Verifier 完成: %dms", elapsed_ms)
        outcome = result.execution_status
        if outcome in {"unverified", "environment_unavailable", "timed_out", "cancelled"}:
            if outcome == "cancelled":
                state.control.user_cancel = True
                self.context.cancelled = True
            elif outcome == "timed_out":
                state.set_status(RepairTerminalStatus.TIMEOUT, "verification_timeout")
            else:
                state.set_status(RepairTerminalStatus.PENDING_VERIFY, "verification_" + outcome)
            return "done"
        if result.all_passed:
            if result.verification_mode == "static":
                state.set_status(RepairTerminalStatus.PENDING_VERIFY, "static_check_completed")
            else:
                state.set_status(RepairTerminalStatus.FIXED, "verification_passed")
            return "done"
        return "verification_failure"

    def verification_failure(self) -> str:
        stop = self.policy.record_verification_failure(
            self.state, self.context.initial_snapshot, self._stop_loss()
        )
        return "done" if stop else "attempt"
