from __future__ import annotations

import time

from agent_runtime.logging_setup import get_logger
from src.repair.phase_clock import PhaseTimeoutError, RepairPhaseClock
from src.repair.verification.termination import RepairTerminalStatus
from src.state import RepairState, RepairStatus, RetrievedContext, SuspectLocation

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
            state.control.pending_approval_id = exc.approval_id
            state.set_status(RepairStatus.WAITING_APPROVAL, "sandbox_approval_required")
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
            from src.repair.graph import run_repair_graph

            cancelled = run_repair_graph(
                self.session, state, initial_snapshot, phase_clock, resume=checkpoint is not None
            )
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
