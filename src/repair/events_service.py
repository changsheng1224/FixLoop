from __future__ import annotations

import threading
import time

from agent_runtime.logging_setup import get_logger
from src.repair.timing_schema import finalize_phases, set_repair_total_ms
from src.repair.verification.termination import (
    RepairTerminalStatus,
    finalize_repair_state,
)
from src.state import (
    RepairState,
)

log = get_logger("repair.pipeline")


def _split_grep_path_line(line: str) -> tuple[str, str, str] | None:
    """Parse ``path:lineno:text`` without breaking Windows drive letters (E8)."""
    import re

    m = re.match("^(.+?):(\\d+):(.*)$", line)
    if not m:
        return None
    return (m.group(1), m.group(2), m.group(3))


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


log = get_logger("orchestrator")

DEFAULT_REPAIR_TIMEOUT_S = 180


class RepairEvents:
    def __init__(self, session):
        self.session = session

    def begin_repair_trace(self, state: RepairState) -> None:
        import uuid

        from agent_runtime.run_coordination import RunCoordinator

        if not state.repair_run_id:
            state.repair_run_id = "repair-" + uuid.uuid4().hex
        coordinator = RunCoordinator(
            self.session.repo_root,
            state.repair_run_id,
            state.repair_run_id,
            state_root=str(
                getattr(getattr(self.session.patcher, "tool_context", None), "state_root", "") or ""
            ),
        )
        coordinator.acquire()
        self.session.entry_coordinator = coordinator
        heartbeat_stop = threading.Event()

        def maintain_entry_owner():
            while not heartbeat_stop.wait(coordinator.lease_seconds / 3):
                try:
                    coordinator.heartbeat()
                except Exception:
                    return

        heartbeat_worker = threading.Thread(
            target=maintain_entry_owner, name="fixloop-entry-owner", daemon=True
        )
        heartbeat_worker.start()
        try:
            self.initialize_repair_trace(state)
        except BaseException:
            current = coordinator.store.snapshot(state.repair_run_id)
            if current.owner_token == coordinator.lease.owner_token:
                current = coordinator.finish(
                    "recovery_required", error_code="repair_initialization_interrupted"
                )
                state.control.coordination_status = current.status
            raise
        finally:
            heartbeat_stop.set()
            heartbeat_worker.join(timeout=1)

    def telemetry(self):
        from src.repair.telemetry_service import RepairTelemetryService

        return RepairTelemetryService(
            self.session.repo_root,
            self.session.repair_ctx,
            self.session.patcher,
            self.session.active_agents,
            self.session.l1_prompt_cache_key,
            self.session.coordination.on_harness_event,
        )

    def initialize_repair_trace(self, state):
        self.telemetry().initialize(state)
        run_id = state.repair_run_id
        try:
            from src.collaboration.repair_runtime import RepairCollaborationRuntime

            self.session.collaboration_runtime = RepairCollaborationRuntime(
                self.session.repo_root,
                run_id,
                state,
                state_root=str(
                    getattr(getattr(self.session.patcher, "tool_context", None), "state_root", "")
                    or ""
                ),
            )
        except Exception as exc:
            self.session.collaboration_runtime = None
            state.agent_errors.setdefault("collaboration_runtime", str(exc)[:500])
        if (
            self.session.repair_ctx is not None
            and self.session.repair_ctx.worktree_handle is not None
        ):
            self.session.repair_ctx.worktree_initial_snapshot = (
                self.session.transaction.snapshot_repo()
            )
        from src.repair.plan_binding import RepairPlanBinding

        self.session.plan_binding = RepairPlanBinding(self.session, state, defer_plan=True)

    def end_repair_trace(self, state: RepairState) -> None:
        ctx = self.session.repair_ctx
        binding = getattr(self.session, "plan_binding", None)
        coordinator = getattr(binding, "coordinator", None)
        if coordinator is not None and getattr(coordinator, "lease", None) is not None:
            try:
                backend = getattr(self.session.sandbox_context, "sandbox_backend", None)
                if backend is not None and hasattr(backend, "close"):
                    self.session.sandbox_backend_close_attempted = True
                    backend.close()
                token = coordinator.cancel_token or (
                    getattr(ctx, "cancel_token", None) if ctx is not None else None
                )
                service = getattr(self.session, "verify_service", None)
                if service is not None and (not service.cleanup_confirmed):
                    self.session.verification.block_unconfirmed_verification(
                        state, "verification cleanup unconfirmed"
                    )
                    coordinator.finish(
                        "recovery_required", error_code="verification_cleanup_unconfirmed"
                    )
                elif state.control.coordination_status == "recovery_required":
                    coordinator.finish(
                        "recovery_required", error_code="rollback_outcome_unconfirmed"
                    )
                elif token is not None and token.is_cancelled:
                    self.session.lifecycle.cancel_run_resources(state, finalize=True)
                elif str(getattr(state, "status", "") or "").lower() in {"failed", "error"}:
                    snapshot = coordinator.finish("failed", error_code="repair_failed")
                    state.control.coordination_status = snapshot.status
                else:
                    snapshot = coordinator.finish("released")
                    state.control.coordination_status = snapshot.status
                if state.control.coordination_status == "recovery_required":
                    state.set_status("recovery_required", "cleanup_or_write_unconfirmed")
            except Exception as exc:
                state.agent_errors.setdefault("coordination_finalize", str(exc)[:500])
                state.control.coordination_status = "recovery_required"
                state.set_status("recovery_required", "coordination_finalize_unconfirmed")
            previous = state.recovery_outcome
            binding.publish_recovery(
                stage=previous.get("stage", "resources"),
                reason_code=previous.get("reason_code", ""),
            )
        if state.control.coordination_status != "recovery_required":
            self.session.lifecycle.maybe_leave_worktree(cancelled=False)
        elif ctx is not None and ctx.worktree_handle is not None:
            state.execution.worktree_recovery = ctx.worktree_handle.as_dict()
            state.node_timings["worktree_recovery"] = state.execution.model_dump()[
                "worktree_recovery"
            ]
        runtime = getattr(self.session, "collaboration_runtime", None)
        if runtime is not None and hasattr(runtime, "close"):
            try:
                runtime.close()
            except Exception as exc:
                state.agent_errors.setdefault("collaboration_runtime_close", str(exc)[:500])
        self.telemetry().finalize(state)

    def reset_token_tracking(self):
        self.telemetry().reset_token_tracking()

    def attach_token_usage(self, state):
        self.telemetry().attach_token_usage(state)

    def attach_rejection_stats(self, state):
        self.telemetry().attach_rejection_stats(state)

    def progress_emitter(self):
        em = getattr(self.session, "progress", None)
        if em is not None:
            return em
        from src.repair.progress import progress_emitter_from_env

        em = progress_emitter_from_env(config=self.session.repair_config)
        self.session.progress = em
        return em

    def emit_repair_span(self, name: str, payload: dict | None = None) -> None:
        """Trace span（与 ProgressEmitter 阶段名对齐；primary 无 Loc/Ret）。"""
        try:
            handle = getattr(self.session.repair_ctx, "worktree_handle", None)
            if handle is not None:
                from agent_runtime.worktree import refresh_worktree_lease

                refresh_worktree_lease(handle)
        except Exception:
            pass
        try:
            ctx = getattr(self.session, "repair_ctx", None)
            tracer = getattr(ctx, "repair_tracer", None) if ctx is not None else None
            if tracer is not None:
                tracer.emit("orchestrator", name, payload or {})
        except Exception:
            pass
        try:
            self.progress_emitter().emit(name, summary=str((payload or {}).get("summary", name)))
        except Exception:
            pass

    def checkpoint_progress(self, state: RepairState) -> None:
        """patch/verify 回合中落盘，支持中断后续跑。"""
        decision = state.control.repair_failure_decision
        if isinstance(decision, dict):
            state.control.checkpoint_next_action = decision.get("next_action", "")
        try:
            self.save_repair_checkpoint(state)
        except Exception:
            pass
        if state.repair_run_id:
            try:
                from src.repair.checkpoint_load import save_repair_checkpoint

                save_repair_checkpoint(
                    state,
                    self.session.repo_root,
                    state_root=str(
                        getattr(
                            getattr(self.session.patcher, "tool_context", None), "state_root", ""
                        )
                        or ""
                    ),
                )
            except Exception:
                pass

    def restore_blackboard_snapshot(self, snapshot: dict | None) -> None:
        ctx = self.session.asks.active_repair_ctx()
        if ctx.blackboard is None:
            return
        from src.repair.blackboard_merge import restore_blackboard_from_snapshot

        restore_blackboard_from_snapshot(ctx.blackboard, snapshot)

    def finalize_repair_run(self, state: RepairState, t_start: float) -> RepairState:
        finalize_repair_state(state)
        self.session.coordination.on_collaboration_phase(
            state,
            "done"
            if state.status in {RepairTerminalStatus.FIXED, RepairTerminalStatus.PENDING_VERIFY}
            else "failed",
            "repair finalized",
        )
        if state.status == "fixed" and state.repair_plan and state.repair_plan.issue_type:
            try:
                from src.repair.precedent import RepairPrecedentStore

                store = RepairPrecedentStore(
                    self.session.repo_root,
                    access_context=getattr(self.session.patcher, "memory_access", None),
                    state_root=str(
                        getattr(
                            getattr(self.session.patcher, "tool_context", None), "state_root", ""
                        )
                        or ""
                    ),
                )
                summary = _build_precedent_summary(state)
                case_id = getattr(self.session, "case_id", "") or ""
                store.upsert(state.repair_plan.issue_type, summary, case_id=case_id)
            except Exception:
                pass
        total_ms = int((time.time() - t_start) * 1000)
        set_repair_total_ms(state.node_timings, total_ms)
        finalize_phases(state.node_timings)
        lock = getattr(self.session, "edit_lock", None)
        if lock is not None:
            state.node_timings["unread_write_reject_count"] = int(
                getattr(lock, "unread_write_reject_count", 0) or 0
            )
            state.node_timings["apply_path_reject_count"] = int(
                getattr(lock, "apply_path_reject_count", 0) or 0
            )
        self.save_repair_checkpoint(state)
        self.attach_token_usage(state)
        self.attach_rejection_stats(state)
        if state.repair_run_id:
            try:
                from src.repair.checkpoint_load import save_repair_checkpoint

                save_repair_checkpoint(
                    state,
                    self.session.repo_root,
                    state_root=str(
                        getattr(
                            getattr(self.session.patcher, "tool_context", None), "state_root", ""
                        )
                        or ""
                    ),
                )
            except Exception:
                pass
        self.end_repair_trace(state)
        self.checkpoint_progress(state)
        self.push_repair_metrics(state)
        self.progress_emitter().emit(
            "repair_finished", summary=f"status={state.status} total_ms={total_ms}"
        )
        log.info("总耗时: %dms, status=%s", total_ms, state.status)
        return state

    def save_repair_checkpoint(self, state) -> None:
        """写入 L2 repair_state.json + checkpoint.json（供 --resume-repair + 审计）。"""
        budget = getattr(self.session, "model_budget", None)
        if budget is not None:
            state.execution.model_budget = budget.snapshot()
            state.execution.model_deadline = self.session.model_deadline.snapshot()
            state.node_timings["model_budget"] = state.execution.model_dump()["model_budget"]
            state.node_timings["model_deadline"] = state.execution.model_dump()["model_deadline"]
        try:
            import json
            import time

            from agent_runtime.state_root import state_root_for

            repair_dir = (
                state_root_for(
                    self.session.repo_root,
                    str(
                        getattr(
                            getattr(self.session.patcher, "tool_context", None), "state_root", ""
                        )
                        or ""
                    ),
                )
                / ".agent"
                / "repairs"
            )
            run_id = getattr(state, "repair_run_id", "") or ""
            sub_dir = repair_dir / run_id if run_id else repair_dir
            sub_dir.mkdir(parents=True, exist_ok=True)
            state_path = sub_dir / "repair_state.json"
            state_payload = state.to_dict()
            state_payload["saved_at"] = time.time()
            from agent_runtime.atomic_io import atomic_write_text

            atomic_write_text(state_path, json.dumps(state_payload, ensure_ascii=False, indent=2))
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
            atomic_write_text(cp_path, json.dumps(cp_payload, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def push_repair_metrics(self, state) -> None:
        """推送 repair 指标到 Prometheus registry（静默失败）。"""
        try:
            from agent_runtime.metrics import get_registry
            from agent_runtime.observability.labels import low_cardinality_labels

            registry = get_registry()
            status = state.status or "unknown"
            registry.counter_inc(
                "fixloop_repair_status", labels=low_cardinality_labels(status=status)
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
                cache_rate = (state.node_timings.get("runtime_metrics") or {}).get("cache_hit_rate")
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
