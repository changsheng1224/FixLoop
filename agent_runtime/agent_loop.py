"""Agent 控制循环：感知 → 决策 → 行动 → 记录 → 循环。

停机后产出 task_state.json + trace.jsonl + report.json（含 node_timings 耗时分布）。
"""

import json
import time as _time
import uuid

from agent_runtime.cancellation import CancelledError, run_with_cancellation
from agent_runtime.errors import ContextBuildBlockedError, ContextTooLargeError
from agent_runtime.loop_limits import max_parse_attempts
from agent_runtime.loop_protocols import (
    ProtocolHooks,
    ProtocolRuntime,
    ProtocolState,
    handle_parse_retry,
    native_model_turn,
    validate_final_answer,
    xml_final_retry,
    xml_invalid_tool_retry,
    xml_model_turn,
)
from agent_runtime.model_timing import (
    ModelCallTiming,
    emit_model_timing_events,
)
from agent_runtime.providers.retry_policy import RateLimitExceededError
from agent_runtime.react_phases import ReactPath, ReactPhase
from agent_runtime.step_clock import StepClock, StepTimeoutError
from agent_runtime.step_guard import StepGuard
from agent_runtime.stop_reasons import StopReason
from agent_runtime.terminal_tool import TerminalToolAcceptedError
from agent_runtime.tool_step_runtime import (
    ToolStepHooks,
    ToolStepRuntime,
    ToolStepState,
    run_tool_step,
    tool_step_flow,
)


class AgentLoop:
    """Agent 控制循环。管理对话回合，统计步数，产出 trace 工件。"""

    def __init__(self, agent, max_steps: int | None = None, *, stream: bool = False):
        self.agent = agent
        self._tool_state = ToolStepState()
        self._protocol_state = ProtocolState()
        self._observation_recorder = self._new_observation_recorder()
        agent._loop = self
        self.max_steps = max_steps or agent.config.max_steps
        self.stop_reason = ""
        self._task_state = None
        self._store = None
        self._stream_enabled = stream
        self._call_timings: list[ModelCallTiming] = []
        self._tier_counts: dict[str, int] = {}
        self._tier_tools: dict[str, dict[str, int]] = {"host": {}, "container": {}}
        self._context_section_totals: dict[str, int] = {}
        self._context_built_count = 0
        self._context_cut_count = 0
        self._last_cache_key = ""
        self._cache_key_changes = 0
        self._context_selected_count = 0
        self._context_dropped_count = 0
        self._context_contract_failures = 0
        self._context_policy_versions: dict[str, int] = {}
        self._plan_todos: list[dict] = []
        self._step_guard = StepGuard()
        self._last_dream_stats: dict[str, int] = {}
        self._max_native_recovery_turns = max(
            2, min(6, int(getattr(agent.config, "max_recovery_attempts", 0) or 3))
        )
        from agent_runtime.repair_run import RunTerminalGuard

        self._terminal_guard = RunTerminalGuard()
        self._stream_seq = 0
        from agent_runtime.budget_manager import BudgetManager
        from agent_runtime.latency_controller import LatencySLOController
        from agent_runtime.repair_runtime import ExecutionDeadline, RepairBudget

        self._repair_deadline = ExecutionDeadline(
            (
                agent.config.effective_deadline()["repair_s"]
                if hasattr(agent.config, "effective_deadline")
                else getattr(agent.config, "repair_wall_timeout_s", 0)
            )
            or 0
        )
        agent._repair_deadline = self._repair_deadline
        self._repair_budget = RepairBudget(
            max_turns=self.max_steps,
            max_tool_calls=getattr(agent.config, "max_tool_calls", 0) or 0,
            max_write_calls=getattr(agent.config, "max_write_calls", 0) or 0,
            max_verify_calls=getattr(agent.config, "max_verify_calls", 0) or 0,
            max_recovery_attempts=getattr(agent.config, "max_recovery_attempts", 0) or 0,
        )
        self._budget_manager = getattr(
            agent, "_run_budget_manager", None
        ) or BudgetManager.from_config(agent.config)
        self._budget_turns_seen = 0
        self._latency_controller = LatencySLOController(
            getattr(agent.config, "slo", None),
            getattr(agent.config, "degradation", None),
        )
        self.agent.session["runtime_budget"] = self._budget_manager.snapshot()

    @property
    def stop_reason(self) -> str:
        return self._tool_state.stop_reason

    @stop_reason.setter
    def stop_reason(self, value: str) -> None:
        self._tool_state.stop_reason = value

    def _protocol_runtime(self) -> ProtocolRuntime:
        return ProtocolRuntime(
            agent=self.agent,
            state=self._protocol_state,
            control=self._tool_state,
            guard=self._step_guard,
            stream_enabled=self._stream_enabled,
            hooks=ProtocolHooks(
                abort_if_cancelled=self._abort_if_cancelled,
                accumulate_context_stats=self._accumulate_context_stats,
                apply_call_usage_meta=self._apply_call_usage_meta,
                begin_edit_lock_turn=self._begin_edit_lock_turn,
                budget_payload=self._budget_payload,
                budget_reserve=self._budget_reserve,
                complete_run=self._complete_run,
                emit=self._emit,
                emit_stream_event=self._emit_stream_event,
                get_task_summary_text=self._get_task_summary_text,
                invoke_model_call=self._invoke_model_call,
                maybe_step_timeout=self._maybe_step_timeout,
                native_tool_names=self._native_tool_names,
                notify=self._notify,
                notify_react_phase=self._notify_react_phase,
                record_model_timings=self._record_model_timings,
                sleep_with_deadline=self._sleep_with_deadline,
                cancel_token=lambda: self._cancel_token,
            ),
        )

    def _tool_runtime(self) -> ToolStepRuntime:
        return ToolStepRuntime(
            agent=self.agent,
            state=self._tool_state,
            guard=self._step_guard,
            budget=self._repair_budget,
            deadline=self._repair_deadline,
            recorder=self._observation_recorder,
            todos=self._plan_todos,
            progress=getattr(self, "_turn_progress", None),
            batch=getattr(self, "_active_tool_batch", None),
            hooks=ToolStepHooks(
                abort_if_cancelled=self._abort_if_cancelled,
                advance_todo=self._advance_todo,
                block_grounded_finish=self._block_grounded_finish,
                budget_allows_tool=self._budget_allows_tool,
                budget_reserve=self._budget_reserve,
                emit=self._emit,
                enter_convergence_gate=self._enter_convergence_gate,
                grant_read_reserve=self._grant_read_reserve,
                has_targeted_read_reserve=self._has_targeted_read_reserve,
                matching_read_reservation=self._matching_read_reservation,
                notify=self._notify,
                notify_react_phase=self._notify_react_phase,
                persist_step_checkpoint=self._persist_step_checkpoint,
                record_tool_outcome=self._record_tool_outcome,
                set_patch_recovery=self._set_patch_recovery,
                sync_patcher_grounding=self._sync_patcher_grounding,
            ),
        )

    def _run_tool_step(self, ts, tool_name, tool_args, **kwargs):
        return run_tool_step(self._tool_runtime(), ts, tool_name, tool_args, **kwargs)

    def _tool_step_flow(self, ts, tool_name, tool_args, **kwargs):
        return tool_step_flow(self._tool_runtime(), ts, tool_name, tool_args, **kwargs)

    def _accumulate_context_stats(self, meta: dict) -> None:
        """从 context_built metadata 累积 section 统计 + cache hit rate。"""
        self._context_built_count += 1
        sections = meta.get("sections") or meta.get("context_sections") or {}
        for name, tokens in sections.items():
            try:
                self._context_section_totals[name] = self._context_section_totals.get(
                    name, 0
                ) + int(tokens)
            except (ValueError, TypeError):
                pass
        cuts = meta.get("cuts") or []
        self._context_cut_count += len(cuts)
        self._context_selected_count += len(meta.get("selected_context_ids", []) or [])
        self._context_dropped_count += len(meta.get("dropped_context_ids", []) or [])
        policy_version = str(meta.get("context_policy_version", "") or "")
        if policy_version:
            self._context_policy_versions[policy_version] = (
                self._context_policy_versions.get(policy_version, 0) + 1
            )
        compression = meta.get("compression_pipeline", {}) or {}
        if compression.get("contract_ok") is False:
            self._context_contract_failures += 1
        cache_key = str(meta.get("prompt_cache_key", "") or "")
        if self._last_cache_key and cache_key != self._last_cache_key:
            self._cache_key_changes += 1
        self._last_cache_key = cache_key
        # emit 压缩触发事件
        pipe = meta.get("compression_pipeline") or {}
        for ev in pipe.get("compression_events", []):
            self._emit("compression_triggered", ev)

    def _build_memory_health(self) -> dict:
        """构建 report.json 中的 memory_health 字段（合并 Dream stats）。"""
        try:
            from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES
            from agent_runtime.features.memory.durable import DurableMemoryStore

            session = self.agent.session or {}
            episodic = session.get("episodic_notes", [])
            store = DurableMemoryStore(self.agent._cwd)
            prefs = store.get_preferences()
            avg_conf = round(sum(p.confidence for p in prefs) / max(len(prefs), 1), 2)

            dream = self._last_dream_stats if hasattr(self, "_last_dream_stats") else {}
            return {
                "episodic_notes": len(episodic),
                "episodic_cap": MAX_EPISODIC_NOTES,
                "durable_entries": sum(
                    1 for _ in store.topics_dir.glob("*.md") if store.topics_dir.is_dir()
                ),
                "avg_confidence": avg_conf,
                "dream_deduped": dream.get("deduped", 0),
                "dream_expired": dream.get("expired", 0),
                "dream_trimmed": dream.get("trimmed", 0),
                "dream_durable_gc": dream.get("durable_gc", 0),
                "dream_promotion_suggestions": dream.get("promotion_suggestions", 0),
                "dream_routing_entries": dream.get("routing_entries", 0),
                "dream_total_before": dream.get("total_before", 0),
                "dream_total_after": dream.get("total_after", 0),
                "governance": {
                    key: dream.get(key, 0)
                    for key in (
                        "normalized",
                        "supported",
                        "verified",
                        "promoted",
                        "demoted",
                        "stale_marked",
                        "conflicts_detected",
                        "conflicts_resolved",
                        "conflicts_unresolved",
                        "policy_shadowed",
                        "probe_attempts",
                        "rejected",
                        "recall_hits",
                    )
                },
            }
        except Exception:
            return {}

    def _build_context_summary(self) -> dict:
        """构建 report.json 中的 context_summary 字段。"""
        build_count = self._context_built_count
        cache_hit_rate = 0.0
        if build_count > 0:
            cache_hit_rate = round(1.0 - (self._cache_key_changes / build_count), 3)
        return {
            "sections": dict(self._context_section_totals),
            "build_count": build_count,
            "cut_count": self._context_cut_count,
            "cache_hit_rate": cache_hit_rate,
            "selected_context_count": self._context_selected_count,
            "dropped_context_count": self._context_dropped_count,
            "compression_contract_failures": self._context_contract_failures,
            "policy_versions": dict(self._context_policy_versions),
        }

    @property
    def _cancel_token(self):
        return getattr(self.agent, "cancel_token", None)

    def _abort_if_cancelled(
        self,
        ts,
        *,
        phase: str,
        in_flight: str = "",
    ) -> str | None:
        token = self._cancel_token
        if token is None or not token.is_cancelled:
            return None
        inflight = in_flight or self._tool_state.in_flight_tool
        return self._finish_user_cancel(ts, phase=phase, in_flight=inflight)

    def _finish_user_cancel(self, ts, *, phase: str, in_flight: str = "") -> str:
        inflight = in_flight or self._tool_state.in_flight_tool
        if ts.stop_reason != StopReason.USER_CANCEL.value:
            ts.stop_user_cancel(in_flight=inflight, phase=phase)
        self._emit(
            "run_cancelled",
            {
                "stop_reason": StopReason.USER_CANCEL.value,
                "cancel_phase": phase,
                "in_flight_tool": inflight,
                "tool_steps": ts.tool_steps,
            },
        )
        try:
            from agent_runtime.checkpoint import create_checkpoint

            create_checkpoint(
                self.agent,
                ts,
                ts.user_request,
                trigger="user_cancel",
                in_flight_tool=inflight,
            )
        except Exception:
            pass
        self._cancel_all_todos()
        return self._complete_run(ts, "<final>用户已取消当前任务。</final>")

    def _invoke_model_call(self, fn):
        token = self._cancel_token
        if token is None:
            return fn()
        return run_with_cancellation(fn, token)

    # ---- 停机与 trace 收尾 ----

    def _sync_stop_reason(self, ts) -> None:
        self.stop_reason = ts.stop_reason or self.stop_reason

    def _run_finished_payload(self, ts) -> dict:
        from agent_runtime.tool_rejection import build_rejection_observability_payload

        payload = {"stop_reason": ts.stop_reason or self.stop_reason}
        detail = ts.node_timings.get("stop_reason_detail", "")
        if detail:
            payload["stop_reason_detail"] = detail
        payload.update(build_rejection_observability_payload(ts.rejection_report_fields()))
        return payload

    def _emit_run_finished(self, ts) -> None:
        self._emit("run_finished", self._run_finished_payload(ts))

    def _complete_run(
        self,
        ts,
        answer: str,
        *,
        recording: dict | None = None,
    ) -> str:
        """TaskState 已写入终态后：同步 stop_reason、可选 recording、落盘。"""
        self._close_turn_progress(status=str(ts.status))
        self._sync_stop_reason(ts)
        runtime_snapshot = ts.runtime_contract or {}
        if not runtime_snapshot.get("terminal"):
            try:
                ts.finalize_runtime(
                    str(ts.status),
                    stop_reason=str(ts.stop_reason or self.stop_reason or "terminal"),
                )
            except ValueError as exc:
                self._emit(
                    "runtime_contract_violation",
                    {"detail": str(exc), "phase": str(ts.phase)},
                )
        from agent_runtime.repair_run import attribute_failure

        memory = self.agent.session.get("memory", {}) or {}
        working = memory.get("working", {}) or {}
        repair_context = working.get("repair_context", {}) or {}
        ts.failure_attribution = attribute_failure(
            stop_reason=ts.stop_reason,
            observations=self.agent.session.get("tool_observations", []),
            changed_files=list(repair_context.get("changed_files", []) or []),
            evidence_count=len(working.get("evidence_ledger", []) or []),
        )
        terminal_status = ts.stop_reason or ts.status or "completed"
        if not self._terminal_guard.try_finish(
            ts.run_id,
            terminal_status,
            self._run_finished_payload(ts),
        ):
            late = self._terminal_guard.late_events[-1]
            self._emit(late.kind, late.payload)
            return answer
        terminal_event = self._terminal_guard.event
        if terminal_event is not None:
            self._emit("run_terminal", terminal_event.payload)
        if recording is not None:
            self._notify_react_phase(
                ReactPhase.RECORDING,
                step=recording["step"],
                path=recording["path"],
                tool=recording.get("tool"),
                callback=recording.get("callback"),
            )
            self._notify("on_final_answer", recording.get("callback"), text=answer)
        self._emit_run_finished(ts)
        self._finalize_run(ts)
        return answer

    def _finish_answer(self, ts, answer: str, *, recording=None, validation_error="") -> str:
        """Record an accepted answer and use the common run finalization path."""
        self.agent.record({"role": "assistant", "content": answer})
        if validation_error:
            self._emit("json_validation_warning", {"error": validation_error})
        if not self.stop_reason:
            ts.finish_success(answer)
        return self._complete_run(ts, answer, recording=recording)

    def _finish_terminal_tool(self, ts, error, *, recording=None) -> str:
        self._emit(
            "terminal_tool_accepted",
            {"tool": error.tool_name, "payload_chars": len(error.payload)},
        )
        return self._finish_answer(ts, error.payload, recording=recording)

    def _check_run_deadline(self, ts) -> str | None:
        if not self._repair_deadline.expired():
            return None
        ts.stop_with_reason(
            StopReason.DEADLINE_EXCEEDED,
            "stopped",
            detail="repair wall-clock deadline exceeded",
        )
        return self._complete_run(ts, "<final>已达到 repair 全局执行期限，当前任务停止。</final>")

    def _finish_step_timeout(self, ts, error, *, clock=None) -> str:
        if not isinstance(error, StepTimeoutError):
            raise error
        ts.stop_step_timeout(error.timeout_s, error.step)
        elapsed_ms = clock.elapsed_ms() if clock is not None else 0
        self._emit(
            "step_timeout",
            {
                "step": error.step,
                "step_timeout_s": error.timeout_s,
                "elapsed_ms": elapsed_ms,
                "path": error.path,
            },
        )
        return self._complete_run(
            ts,
            (
                f"<final>单步执行超时（{error.timeout_s} 秒），"
                f"step={error.step}，path={error.path or 'unknown'}。</final>"
            ),
        )

    def _maybe_step_timeout(self, ts, clock, step: int, path: ReactPath):
        try:
            clock.check(step=step, path=path)
        except StepTimeoutError as e:
            return self._finish_step_timeout(ts, e, clock=clock)
        return None

    def _stop_for_api_error(self, ts, error: Exception) -> str | None:
        from agent_runtime.providers.contracts import ProviderError, normalize_provider_error

        if isinstance(error, RateLimitExceededError):
            ts.stop_with_reason(StopReason.RATE_LIMITED, "stopped", detail=str(error))
            return self._complete_run(ts, f"<final>API 限流：{error}</final>")
        from agent_runtime.providers.circuit_breaker import CircuitBreakerOpenError

        if isinstance(error, CircuitBreakerOpenError):
            ts.stop_with_reason(StopReason.CIRCUIT_BREAKER, "stopped", detail=str(error))
            return self._complete_run(ts, f"<final>API 熔断：{error}</final>")
        provider_error = error if isinstance(error, ProviderError) else None
        if provider_error is None and isinstance(error, TimeoutError | OSError):
            provider_error = normalize_provider_error(error)
        if isinstance(provider_error, ProviderError):
            self._emit("provider_error", provider_error.to_trace_payload())
            ts.stop_with_reason(StopReason.API_ERROR, "failed", detail=str(provider_error))
            return self._complete_run(
                ts,
                f"<final>Provider 错误（{provider_error.code.value}）：{provider_error}</final>",
            )
        return None

    def _circuit_trace_listener(self, event: str, payload: dict) -> None:
        self._emit(event, payload)

    # ---- ReAct / 计时 ----

    def _record_model_timings(self, ts, timings: list, *, default_attempt: int = 1) -> None:
        if not timings:
            return
        self._call_timings.extend(timings)
        for timing in timings:
            if isinstance(timing, dict):
                total_ms = int(timing.get("total_ms", 0) or 0)
                ttft_ms = int(timing.get("ttft_ms", 0) or 0)
            else:
                total_ms = int(getattr(timing, "total_ms", 0) or 0)
                ttft_ms = int(getattr(timing, "ttft_ms", 0) or 0)
            self._latency_controller.record("model", total_ms)
            self._latency_controller.record("ttft", ttft_ms)
            self._emit("latency_observed", {"kind": "model", "duration_ms": total_ms})
            self._emit("latency_observed", {"kind": "ttft", "duration_ms": ttft_ms})
        ttft_total = emit_model_timing_events(
            lambda event, payload: self._emit(
                event, {**payload, "model": getattr(self.agent.config, "model", "")}
            ),
            timings,
            default_attempt=default_attempt,
        )
        ts.node_timings["ttft_ms_total"] = (
            int(ts.node_timings.get("ttft_ms_total", 0) or 0) + ttft_total
        )

    def _notify(self, method: str, callback, **kwargs: object) -> None:
        """统一回调入口：XML 与 Native 路径共用。

        若 callback 为 None 或未实现 method，静默跳过。
        """
        if callback is None:
            return
        fn = getattr(callback, method, None)
        if fn is not None:
            fn(**kwargs)

    def _emit_stream_event(
        self, kind: str, payload: dict, *, phase: str = "", turn: int = 0
    ) -> None:
        """Emit replayable runtime stream metadata without exposing raw CoT."""
        if not self._stream_enabled:
            return
        self._stream_seq += 1
        self._emit(
            "stream_event",
            {
                "stream_seq": self._stream_seq,
                "kind": kind,
                "phase": phase,
                "turn": turn,
                "replayable": kind not in {"token_delta"},
            },
        )

    def _notify_react_phase(
        self,
        phase,
        *,
        step: int,
        path: ReactPath,
        tool: str | None = None,
        callback=None,
    ) -> None:
        progress = getattr(self, "_turn_progress", None)
        if progress is not None:
            progress.phase = str(phase)
            progress.emit("turn_phase", status="running")
        from agent_runtime.react_phases import build_react_phase_payload
        from agent_runtime.step_engine import StepEngine

        if self._task_state is not None:
            StepEngine(self._task_state, self._emit).enter(
                str(phase), step=step, path=path, tool=tool or ""
            )
        self._emit(
            "react_phase",
            build_react_phase_payload(phase, step=step, path=path, tool=tool),
        )
        self._notify(
            "on_react_phase",
            callback,
            phase=str(phase),
            step=step,
            max_steps=self.max_steps,
            tool=tool or "",
        )

    def _step_timeout_limit_s(self) -> int:
        if hasattr(self.agent.config, "effective_deadline"):
            return int(self.agent.config.effective_deadline()["step_s"] or 0)
        return int(getattr(self.agent.config, "step_timeout_s", 0) or 0)

    def _sleep_with_deadline(self, seconds: float) -> bool:
        """Sleep only within the remaining repair deadline."""
        remaining = self._repair_deadline.remaining_s()
        if remaining is not None and remaining <= 0:
            return False
        delay = float(seconds) if remaining is None else min(float(seconds), remaining)
        if delay > 0:
            _time.sleep(delay)
        return not self._repair_deadline.expired()

    def _budget_payload(self) -> dict:
        """Return the current unified budget view for prompts and trace."""
        payload = self._budget_manager.decision_payload()
        payload["legacy"] = self._repair_budget.summary()
        payload["latency"] = self._latency_controller.summary()
        self.agent.session["runtime_budget"] = self._budget_manager.snapshot()
        return payload

    def _apply_latency_decision(self, max_output_tokens: int) -> dict:
        decision = self._latency_controller.decide(
            remaining_s=self._repair_deadline.remaining_s(),
            max_output_tokens=max_output_tokens,
        )
        self.agent.session["runtime_degradation"] = {
            "skip_optional_context": "skip_optional_context" in decision["actions"],
            "reasons": list(decision["reasons"]),
            "max_output_tokens": int(decision["max_output_tokens"]),
        }
        for reason in decision["reasons"]:
            if reason.endswith("_p95_exceeded"):
                self._emit(
                    "latency_slo_exceeded",
                    {"kind": reason.removesuffix("_p95_exceeded"), "reason": reason},
                )
        if decision["degraded"]:
            self._emit("latency_degraded", decision)
        return decision

    def _budget_check(self, resource: str, amount: float = 1.0) -> bool:
        decision = self._budget_manager.check(resource, amount)
        if decision.allowed:
            return True
        self._emit(
            "budget_exhausted",
            {
                "resource": decision.resource,
                "used": decision.used,
                "limit": decision.limit,
                "action": decision.action,
                "reason": decision.reason,
            },
        )
        return False

    def _budget_reserve(self, resource: str, amount: float = 1.0) -> bool:
        decision = self._budget_manager.reserve(resource, amount)
        self.agent.session["runtime_budget"] = self._budget_manager.snapshot()
        if decision.allowed:
            self._emit(
                "budget_reserved",
                {
                    "resource": decision.resource,
                    "amount": float(amount),
                    "used": decision.used,
                    "limit": decision.limit,
                    "remaining": decision.remaining,
                },
            )
            return True
        self._emit(
            "budget_exhausted",
            {
                "resource": decision.resource,
                "used": decision.used,
                "limit": decision.limit,
                "action": decision.action,
                "reason": decision.reason,
            },
        )
        return False

    def _budget_allows_tool(self, group: str) -> bool:
        if not self._budget_check("tool_calls"):
            return False
        mapping = {"write": "writes", "verify": "verifies", "recovery": "recoveries"}
        specific = mapping.get(group)
        return specific is None or self._budget_check(specific)

    def _grant_read_reserve(
        self,
        path: str,
        *,
        kind: str,
        step: int,
        generation: int = 0,
    ) -> None:
        quota = getattr(self.agent, "quota", None)
        if quota is None or not hasattr(quota, "grant_read_reserve"):
            return
        if quota.grant_read_reserve(path, kind=kind, generation=generation):
            self._emit(
                "post_lock_read_reserved" if kind == "post_lock" else "targeted_read_reserved",
                {
                    "step": step,
                    "path": path,
                    "kind": kind,
                    "generation": generation,
                    "uses": 1,
                },
            )

    def _matching_read_reservation(self, tool_name: str, tool_args: dict) -> dict | None:
        quota = getattr(self.agent, "quota", None)
        if quota is None or not hasattr(quota, "matching_read_reserve"):
            return None
        tool_spec = (self.agent.tools or {}).get(tool_name) or {}
        return quota.matching_read_reserve(tool_name, tool_spec, tool_args)

    def _has_targeted_read_reserve(self) -> bool:
        """Whether a bounded exact reread is currently available."""
        quota = getattr(self.agent, "quota", None)
        if quota is None or not hasattr(quota, "quota_summary"):
            return False
        reserves = (quota.quota_summary() or {}).get("read_reserves") or []
        return any(str(item.get("kind", "")) == "targeted" for item in reserves)

    def _set_patch_recovery(self, kind: str, directive: str, allowed_tools: set[str]) -> None:
        self._tool_state.patch_recovery_kind = str(kind)
        self._tool_state.patch_recovery_directive = str(directive)
        self._tool_state.patch_recovery_allowed_tools = set(allowed_tools)
        self._tool_state.patch_decision_required = True

    def _patcher_grounded(self) -> bool:
        """Return whether this Patcher has read an editable implementation path."""
        runtime = self.agent.session.get("_patcher_runtime", {}) or {}
        if bool(runtime.get("grounded")):
            return True
        try:
            from src.repair.execution.edit_lock import get_active_edit_lock
            from src.repair.localization.localize_quality import _is_test_path

            lock = get_active_edit_lock(getattr(self.agent.tool_context, "root", None))
            if lock is None:
                return False
            return any(
                path in lock.read_set and path in lock.allowed_edit and not _is_test_path(path)
                for path in lock.allowed_edit
            )
        except Exception:
            return False

    def _sync_patcher_grounding(self, tool_name: str, tool_args: dict, result) -> None:
        """Reflect implementation-read evidence into L1 state and action gating."""
        if (getattr(self.agent, "agent_name", "") or "") != "patcher":
            return
        metadata = getattr(result, "metadata", {}) or {}
        if metadata.get("tool_status") != "success" or tool_name != "read_file":
            return
        try:
            from src.repair.execution.edit_lock import get_active_edit_lock, normalize_repo_rel
            from src.repair.localization.localize_quality import _is_test_path

            lock = get_active_edit_lock(getattr(self.agent.tool_context, "root", None))
            if lock is None:
                return
            path = normalize_repo_rel(str(tool_args.get("path") or ""), lock.repo_root)
            grounded_paths = [
                item
                for item in sorted(lock.allowed_edit)
                if item in lock.read_set and not _is_test_path(item)
            ]
            if path not in grounded_paths and not grounded_paths:
                return
            runtime = self.agent.session.setdefault("_patcher_runtime", {})
            runtime["grounded"] = True
            runtime["grounded_paths"] = grounded_paths[:12]
            runtime["patch_required"] = True
            state = getattr(self.agent, "_l2_repair_state", None)
            if state is not None:
                state.control.allowed_edit = sorted(lock.allowed_edit)
                state.node_timings["patcher_grounded"] = True
                state.node_timings["patch_required"] = True
                ledger = ((self.agent.session.get("memory") or {}).get("working") or {}).get(
                    "evidence_ledger", []
                )
                if isinstance(ledger, list):
                    state.node_timings["evidence_ledger"] = [dict(item) for item in ledger[-12:]]
            self._set_patch_recovery(
                "grounded_evidence",
                "已读取实现文件并获得可编辑证据。停止继续探索，立即调用 apply_patch/patch_file；"
                "若确实无法形成补丁，只能声明 cannot_patch 并说明具体原因。",
                {"apply_patch", "patch_file", "finish_repair"},
            )
            self._emit(
                "patcher_grounded",
                {"path": path, "grounded_paths": grounded_paths[:12], "patch_required": True},
            )
        except Exception:
            return

    def _block_grounded_finish(self, tool_name: str, tool_args: dict, result) -> bool:
        """Reject needs_more_context after implementation evidence exists."""
        if tool_name != "finish_repair" or not self._patcher_grounded():
            return False
        status = str(tool_args.get("status") or "").strip().lower()
        if status != "needs_more_context":
            return False
        result.status = "rejected"
        result.error_code = "grounded_finish_blocked"
        result.retryable = False
        result.content = (
            "Error: 已有实现文件证据，不能以 needs_more_context 结束。"
            "请调用 apply_patch/patch_file；若无法修复请改用 cannot_patch。"
        )
        result.metadata.update(
            {
                "tool_status": "rejected",
                "tool_error_code": "grounded_finish_blocked",
                "retryable": False,
                "required_next_action": "apply_patch_or_cannot_patch",
            }
        )
        self._set_patch_recovery(
            "grounded_finish_blocked",
            "已有实现文件证据，needs_more_context 已拒绝。请直接提交补丁，或声明 cannot_patch。",
            {"apply_patch", "patch_file", "finish_repair"},
        )
        self._emit("grounded_finish_blocked", {"status": status})
        return True

    def _native_tool_names(
        self, *, action_required: bool = False, patch_only_recovery: bool = False
    ) -> set[str] | None:
        """Project tool schemas to the current repair phase."""
        is_patcher = (getattr(self.agent, "agent_name", "") or "") == "patcher"
        if is_patcher and action_required:
            names = {"apply_patch", "patch_file", "finish_repair"}
            if self._tool_state.patch_recovery_allowed_tools is not None:
                # Recovery directives are stricter than the normal convergence
                # window.  In particular stale writes expose only the exact
                # reread, while malformed writes expose apply_patch.
                return set(self._tool_state.patch_recovery_allowed_tools)
            # Patcher owns localization.  Once convergence has requested a
            # write, retain a small bounded read window so a newly discovered
            # implementation path is not made unreachable by schema gating.
            if not patch_only_recovery and self._step_guard.localization_reads_available:
                names.update(
                    {
                        "read_file",
                        "grep",
                        "list_files",
                        "search",
                        "ast_parse",
                        "code_lookup",
                        "code_relations",
                        "inspect_file",
                        "find_test",
                    }
                )
            # A truncated response is an action boundary.  Only stale
            # preimage recovery may open one explicitly bounded reread.
            elif not patch_only_recovery and self._has_targeted_read_reserve():
                names.update({"read_file", "ast_parse", "inspect_file"})
            return names
        if not is_patcher or self._step_guard.phase != "converge":
            return None
        writes = {
            "write_file",
            "patch_file",
            "apply_patch",
            "finish_repair",
            "expand_lock",
            "quick_test",
        }
        quota = getattr(self.agent, "quota", None)
        reserves = list((quota.quota_summary() if quota else {}).get("read_reserves") or [])
        has_post_lock = any(item.get("kind") == "post_lock" for item in reserves)
        if has_post_lock or (
            self._step_guard.targeted_read_available
            and not self._tool_state.patch_decision_required
        ):
            writes.add("read_file")
        return writes

    def _enter_convergence_gate(self, reason: str, *, step: int) -> None:
        self._emit(
            "convergence_gate_entered",
            {
                "step": step,
                "reason": reason,
                "reads_since_write": self._step_guard.reads_since_write,
            },
        )
        self._grant_read_reserve("*", kind="targeted", step=step)

    def _budget_reserve_turn(self, turn: int) -> bool:
        if int(turn) <= self._budget_turns_seen:
            return True
        allowed = self._budget_reserve("turns")
        if allowed:
            self._budget_turns_seen = int(turn)
        return allowed

    def _new_observation_recorder(self):
        from agent_runtime.observation_recording import ToolObservationRecorder

        return ToolObservationRecorder(
            session=self.agent.session,
            root=str(getattr(self.agent, "_cwd", "") or ""),
            state_root=str(getattr(self.agent.tool_context, "state_root", "") or ""),
            emit=self._emit,
            on_changed_paths=self._invalidate_tool_exploration,
            on_retrieval=self._observe_tool_retrieval,
        )

    def _invalidate_tool_exploration(self, tool_name):
        if tool_name not in {"write_file", "patch_file", "apply_patch"}:
            service = self.agent.tool_context.exploration_service
            if service is not None:
                service.invalidate("tool_write")

    def _observe_tool_retrieval(self, tool_name, tool_args, retrieval, observation_id):
        if self.agent.tool_context.exploration_mode == "relations" and isinstance(retrieval, dict):
            from agent_runtime.tools import _exploration_service

            _exploration_service(self.agent.tool_context).observe(
                tool_name,
                tool_args,
                retrieval,
                observation_id,
            )

    # ---- 工具执行（XML / Native 共用）----

    def _append_turn_progress(self, event, payload):
        # Progress delivery requires an authoritative trace append. Let IO
        # failures stop dispatch instead of displaying an unrecorded event.
        self._get_store().append_trace_event(payload["run_id"], event, payload)

    def _deliver_turn_progress(self, event, callback):
        progress = getattr(self, "_turn_progress", None)
        if progress is not None:
            self.agent.session["turn_progress"] = progress.checkpoint(
                getattr(self, "_active_tool_batch", None)
            )
        self._notify("on_turn_progress", callback, event=event)

    def _close_turn_progress(self, status="completed"):
        progress = getattr(self, "_turn_progress", None)
        if progress is not None:
            batch = getattr(self, "_active_tool_batch", None)
            if batch is not None:
                from agent_runtime.tool_batch import call_result, cancellation_result

                for call in batch.calls:
                    if call.status in {"queued", "running"}:
                        uncertain = call.status == "running" or call.context.budget_reserved
                        call.context.cancel_token.cancel("turn_closed")
                        call.status = "uncertain" if uncertain else "cancelled"
                        call.result = call_result(call, cancellation_result(uncertain=uncertain))
                        progress.emit(
                            "tool_call_uncertain" if uncertain else "tool_call_cancelled",
                            batch_id=batch.batch_id,
                            call_id=call.call_id,
                            ordinal=call.ordinal,
                            tool_name=call.tool_name,
                            status=call.status,
                            error_code=call.result.error_code,
                        )
                if batch.status in {"pending", "running"}:
                    batch.status = (
                        "uncertain"
                        if any(c.status == "uncertain" for c in batch.calls)
                        else "cancelled"
                    )
                    progress.emit(
                        "tool_batch_completed", batch_id=batch.batch_id, status=batch.status
                    )
            progress.emit("turn_completed", status=status)
            self.agent.session["turn_progress"] = progress.checkpoint(batch)
            self._turn_progress = None
            if getattr(self.agent, "_turn_event_emitter", None) is progress:
                self.agent._turn_event_emitter = None
            self._active_tool_batch = None

    def _run_native_batch(self, ts, calls, *, turn, callback=None, native_content=None):
        from agent_runtime.tool_batch_runtime import (
            BatchExecutionHooks,
            SettledToolStep,
            ToolBatchRunner,
        )

        base_step = ts.tool_steps
        plan = getattr(self.agent, "_plan_session", None)

        def step_flow(call, prepared_result):
            return self._tool_step_flow(
                ts,
                call.tool_name,
                call.arguments,
                step=base_step + call.ordinal + 1,
                path="native",
                callback=callback,
                emit_acting=False,
                call_context=call.context,
                prepared_result=prepared_result,
            )

        def settle_step(flow, result):
            try:
                flow.send(result)
            except StopIteration as done:
                return SettledToolStep(done.value, self._tool_state.last_observation_id)
            raise RuntimeError("tool step yielded twice")

        def activate(batch):
            self._active_tool_batch = batch

        def uncertain():
            self.agent.tool_context.execution_uncertain = True

        def dispatch(name, gated):
            dispatch_tool = getattr(self.agent, "_tool_dispatch", None)
            return dispatch_tool(self.agent._agent_name, name, gated) if dispatch_tool else gated()

        hooks = BatchExecutionHooks(
            step_flow=step_flow,
            settle_step=settle_step,
            execute_serial=lambda call: self.agent.execute_tool(
                call.tool_name,
                call.arguments,
                call_context=call.context,
            ),
            plan_operation=(
                lambda call: plan.tool_operation(
                    self.agent,
                    call.tool_name,
                    call.arguments,
                    call_context=call.context,
                )
            )
            if plan is not None
            else None,
            dispatch=dispatch,
            activate=activate,
            acting=lambda call: self._notify_react_phase(
                ReactPhase.ACTING,
                step=turn,
                path="native",
                tool=call.tool_name,
                callback=callback,
            ),
            uncertain=uncertain,
        )
        runner = ToolBatchRunner(
            context=self.agent.tool_context,
            registry=dict(self.agent.tools),
            allowed_tools=self.agent._tool_names,
            executor=self.agent._get_tool_executor(),
            progress=self._turn_progress,
            session=self.agent.session,
            hooks=hooks,
            cancel_token=self._cancel_token,
            expired=self._repair_deadline.expired,
            tool_timeout_s=float(self.agent.config.effective_deadline()["tool_s"] or 0),
            emit=self._emit,
        )
        results, refs = runner.run(calls, native_content=native_content)
        if (msg := self._abort_if_cancelled(ts, phase="batch_settled")) is not None:
            raise CancelledError("user", answer=msg)
        return results, refs

    # ---- XML 路径辅助 ----

    def _check_xml_loop_limits(self, ts) -> str | None:
        if ts.tool_steps > self.max_steps:
            ts.stop_step_limit(self.max_steps)
            return self._complete_run(
                ts,
                f"<final>已达到最大工具调用步数限制({self.max_steps})，当前任务未完成。</final>",
            )
        limit = max_parse_attempts(self.max_steps)
        if ts.attempts >= limit:
            ts.stop_retry_limit(limit)
            return self._complete_run(
                ts,
                "<final>模型输出格式错误次数过多，已终止。"
                "请检查 System Prompt 中的工具调用格式说明。</final>",
            )
        return None

    def _context_blocked(self, ts, error: ContextBuildBlockedError) -> str:
        self._emit("context_blocked", {"reason": error.reason, **error.metadata})
        self.agent.session["context_blocked"] = {"reason": error.reason, **error.metadata}
        ts.stop_with_reason(StopReason.CONTEXT_BLOCKED, "stopped", detail=error.reason)
        return self._complete_run(ts, error.user_message)

    def _plan_phase(self, user_message: str, *, skip_plan: bool = False) -> None:
        """Plan 阶段：生成 TodoList 并写入 session。

        Args:
            user_message: 用户输入。
            skip_plan: L2 repair 等场景跳过 plan（避免额外 LLM 调用）。
        """
        if skip_plan:
            self._emit("plan_phase", {"source": "skipped"})
            self._plan_todos = []
            return

        todos = self._generate_plan(user_message)
        self._plan_todos = todos
        if todos:
            self.agent.session["plan_todos"] = todos
            self._emit(
                "plan_phase",
                {
                    "source": ("llm" if getattr(self.agent, "light_client", None) else "rule"),
                    "count": len(todos),
                },
            )
            self._emit("plan_created", {"todos": list(todos)})
            self._start_next_todo()
        else:
            self._emit("plan_phase", {"source": "empty"})

    def _generate_plan(self, user_message: str) -> list[dict]:
        """用 light_client 或规则生成 TodoList。"""
        # 尝试 light_client
        light = getattr(self.agent, "light_client", None)
        if light is not None:
            try:
                prompt = (
                    "将以下任务分解为 2-5 个步骤。只输出 JSON 数组：\n"
                    f'[{{"id":"1","content":"...","status":"pending"}},...]\n\n{user_message[:500]}'
                )
                raw = light.complete(prompt, max_new_tokens=256)
                start = raw.find("[")
                end = raw.rfind("]") + 1
                if start >= 0 and end > start:
                    todos = json.loads(raw[start:end])
                    if isinstance(todos, list) and todos:
                        return todos
            except Exception:
                pass
        # 规则 fallback
        msg = user_message.lower()
        todos = []
        i = 1
        if any(w in msg for w in ("error", "fix", "bug", "repair", "修复")):
            todos.append(
                {
                    "id": str(i),
                    "content": "Analyze error and locate suspect code",
                    "status": "pending",
                }
            )
            todos.append(
                {
                    "id": str(i + 1),
                    "content": "Retrieve related context and tests",
                    "status": "pending",
                }
            )
            todos.append(
                {
                    "id": str(i + 2),
                    "content": "Generate and apply fix patch",
                    "status": "pending",
                }
            )
            todos.append(
                {
                    "id": str(i + 3),
                    "content": "Verify fix passes tests",
                    "status": "pending",
                }
            )
        else:
            todos.append({"id": str(i), "content": "Read relevant files", "status": "pending"})
            todos.append({"id": str(i + 1), "content": "Analyze and respond", "status": "pending"})
        return todos

    def _start_next_todo(self) -> None:
        """将下一个 pending todo 标记为 in_progress。"""
        for todo in self._plan_todos:
            if todo.get("status") == "pending":
                todo["status"] = "in_progress"
                self._emit("todo_updated", {"todo": dict(todo)})
                break

    def _advance_todo(self) -> None:
        """将当前 in_progress → done，启动下一个 pending。"""
        for todo in self._plan_todos:
            if todo.get("status") == "in_progress":
                todo["status"] = "done"
                self._emit("todo_updated", {"todo": dict(todo)})
                break
        self._start_next_todo()

    def _cancel_all_todos(self) -> None:
        """将未完成 todo 全部标记为 cancelled。"""
        for todo in self._plan_todos:
            if todo.get("status") in ("pending", "in_progress"):
                todo["status"] = "cancelled"
                self._emit("todo_updated", {"todo": dict(todo)})

    def _persist_step_checkpoint(
        self,
        ts,
        tool_name: str,
        tool_args: dict,
        result_text: str,
        next_user_message: str,
        result,
        *,
        step: int,
        path: ReactPath,
    ) -> None:
        """保存可重入 tool step checkpoint，并立即落盘 session/task_state。"""
        token = getattr(self.agent, "cancel_token", None)
        if token is not None and token.is_cancelled:
            return
        meta = getattr(result, "metadata", None) or {}
        if meta.get("tool_status") != "success":
            return
        try:
            from agent_runtime.checkpoint import create_checkpoint, file_content_hash
            from agent_runtime.session_store import SessionStore

            affected_paths = list(meta.get("affected_paths") or [])
            if tool_name in ("write_file", "patch_file", "apply_patch") and tool_args.get("path"):
                affected_path = str(tool_args["path"])
                if affected_path not in affected_paths:
                    affected_paths.append(affected_path)
            effects = [
                {
                    "path": path,
                    "post_hash": file_content_hash(self.agent._cwd, path),
                }
                for path in affected_paths
            ]
            payload = {
                "resume_kind": "tool_step",
                "step_index": step,
                "path": str(path),
                "tool": tool_name,
                "tool_args": dict(tool_args),
                "tool_result": result_text[:8000],
                "result_metadata": dict(meta),
                "next_user_message": next_user_message[:10000],
                "history_len": len(self.agent.session.get("history", [])),
                "task_state": ts.to_dict(),
                "effects": effects,
            }
            cp = create_checkpoint(
                self.agent,
                ts,
                ts.user_request,
                trigger="step_end",
                last_tool=tool_name,
                step_payload=payload,
            )
            ts.checkpoint_id = cp.get("checkpoint_id", "") if cp else ""
            ts.checkpoint_sequence = int(cp.get("sequence", 0) or 0) if cp else 0
            SessionStore(
                root=self.agent._cwd,
                state_root=str(getattr(self.agent.tool_context, "state_root", "") or ""),
            ).save(self.agent.session)
            self._get_store().write_task_state(ts)
        except Exception:
            pass

    # ---- 入口 ----

    def run(self, user_message: str, callback=None, *, skip_plan: bool = False) -> str:
        # The runtime owner is the run thread, which may differ from the constructor thread.
        self._observation_recorder = self._new_observation_recorder()
        from agent_runtime.budget_manager import BudgetManager
        from agent_runtime.latency_controller import LatencySLOController
        from agent_runtime.log_context import log_context
        from agent_runtime.task_state import TaskState

        resume_state = self._consume_step_resume_state()
        if resume_state is not None:
            self._reset_exploration_on_resume()
            return self._run_from_step_resume(resume_state, callback=callback)

        if self.agent.tool_context.exploration_mode == "relations":
            self.agent.session["exploration_epoch"] = uuid.uuid4().hex

        shared = getattr(self.agent, "shared_run_id", None)
        agent_name = getattr(self.agent, "_agent_name", "") or "agent"
        ts = TaskState.create(
            user_request=user_message,
            run_id=shared,
            session_id=str(self.agent.session.get("id", "") or ""),
            attempt_id="attempt-" + uuid.uuid4().hex[:16],
        )
        l2_agent = getattr(self.agent, "_l2_agent", "") or ""
        if shared and l2_agent:
            ts.task_id = getattr(self.agent, "_l2_task_id", "") or f"{shared}-{agent_name}"
            ts.l2_repair_run_id = getattr(self.agent, "_l2_repair_run_id", "") or shared
            ts.l2_agent = l2_agent
            ts.l2_phase = getattr(self.agent, "_l2_phase", "") or ""
            ts.l2_attempt = int(getattr(self.agent, "_l2_attempt", 0) or 0)
        elif shared:
            ts.task_id = f"{shared}-{agent_name}"
        session_identity = self.agent.session.setdefault("session_identity", {})
        session_identity.update(
            {
                "session_id": ts.session_id,
                "task_id": ts.task_id,
                "run_id": ts.run_id,
                "attempt_id": ts.attempt_id,
            }
        )
        self._task_state = ts
        if agent_name == "patcher":
            self.agent.session.pop("_patcher_runtime", None)
        self._call_timings = []
        self._budget_manager = getattr(
            self.agent, "_run_budget_manager", None
        ) or BudgetManager.from_config(self.agent.config)
        self._budget_turns_seen = 0
        self._latency_controller = LatencySLOController(
            getattr(self.agent.config, "slo", None),
            getattr(self.agent.config, "degradation", None),
        )
        self._protocol_state.retry_count = 0
        self._tool_state.blocked_convergence_reads = 0
        self._tool_state.patch_decision_required = False
        self._tool_state.patch_recovery_directive = ""
        self._tool_state.patch_recovery_allowed_tools = None
        self._tool_state.patch_recovery_kind = ""
        from agent_runtime.repair_runtime import ExecutionDeadline

        self._repair_deadline = ExecutionDeadline(
            (
                self.agent.config.effective_deadline()["repair_s"]
                if hasattr(self.agent.config, "effective_deadline")
                else getattr(self.agent.config, "repair_wall_timeout_s", 0)
            )
            or 0
        )
        self.agent._repair_deadline = self._repair_deadline
        self._repair_budget.turns = 0
        self._repair_budget.tool_calls = 0
        self._repair_budget.writes = 0
        self._repair_budget.verifies = 0
        self._repair_budget.recoveries = 0

        cb = self.agent.circuit_breaker
        cb.add_listener(self._circuit_trace_listener)
        try:
            with log_context(run_id=ts.run_id, agent=agent_name):
                self._emit("run_started")
                from agent_runtime.message_projection import init_run_projection

                init_run_projection(self.agent.session, user_message)
                self.agent.record({"role": "user", "content": user_message})
                self._gen_task_summary(user_message)
                self._plan_phase(user_message, skip_plan=skip_plan)
                self._tool_state.no_progress_steps = 0
                # 重置 StepGuard + 注入任务上下文
                self._step_guard.reset(
                    task_summary=self._get_task_summary_text(),
                    suspect_files=self._extract_suspect_files(),
                    localization_mode=(agent_name == "patcher"),
                )

                answer = self._run_loop(user_message, ts, callback)
                # Memory Dream: ask() 结束后自动维护记忆
                self._run_memory_dream()
                return answer
        finally:
            cb.remove_listener(self._circuit_trace_listener)
            service = self.agent.tool_context.exploration_service
            if service is not None:
                service.close()
                self.agent.tool_context.exploration_service = None

    def _consume_step_resume_state(self) -> dict | None:
        """取出一次性 step resume 状态；不满足条件时保持普通 ask 行为。"""
        resume_state = self.agent.session.get("resume_state")
        if not isinstance(resume_state, dict):
            return None
        if resume_state.get("status") != "step-resumable":
            return None
        checkpoint = resume_state.get("last_checkpoint")
        if not isinstance(checkpoint, dict):
            return None
        self.agent.session.pop("resume_state", None)
        return resume_state

    def _reset_exploration_on_resume(self) -> None:
        if self.agent.tool_context.exploration_mode != "relations":
            return
        service = self.agent.tool_context.exploration_service
        if service is not None:
            service.invalidate("resume")
            service.close()
            self.agent.tool_context.exploration_service = None
        self.agent.session["exploration_epoch"] = uuid.uuid4().hex
        self.agent.session.pop("context_manifest", None)
        memory = self.agent.session.get("memory") or {}
        if isinstance(memory, dict):
            memory.pop("context_manifest", None)

    def _run_from_step_resume(self, resume_state: dict, callback=None) -> str:
        """从最后一个成功 tool step 后继续公共 ReAct 循环。"""
        from agent_runtime.log_context import log_context
        from agent_runtime.task_state import TaskState

        checkpoint = resume_state["last_checkpoint"]
        ts_data = checkpoint.get("task_state") or {}
        ts = TaskState.from_dict(ts_data)
        ts.status = "running"
        ts.stop_reason = ""
        ts.final_answer = ""
        self._task_state = ts
        if self.agent.tool_context.exploration_mode == "relations":
            self._emit(
                "exploration_reset_on_resume",
                {"epoch": self.agent.session.get("exploration_epoch")},
            )
        if checkpoint.get("turn_progress"):
            from agent_runtime.turn_progress import restore_progress

            progress = checkpoint["turn_progress"]
            self.agent.session["turn_progress"] = progress
            plan = getattr(self.agent, "_plan_session", None)
            operations = plan.store.latest("operation", "operation_id").values() if plan else ()
            self.agent.session["turn_progress_replay"] = restore_progress(
                progress,
                operations=operations,
            )
            self._notify(
                "on_turn_progress_replay",
                callback,
                projection=self.agent.session["turn_progress_replay"],
            )
        if checkpoint.get("action_ledger") is not None:
            self.agent.session["action_ledger"] = list(checkpoint.get("action_ledger") or [])[-100:]
        if checkpoint.get("side_effects") is not None:
            self.agent.session["side_effects"] = list(checkpoint.get("side_effects") or [])
        if checkpoint.get("workspace_manifest"):
            self.agent.session["workspace_manifest"] = dict(checkpoint["workspace_manifest"])
        self._restore_runtime_control(checkpoint.get("runtime_control") or {})
        self._call_timings = []
        user_message = checkpoint.get("next_user_message", "")

        cb = self.agent.circuit_breaker
        cb.add_listener(self._circuit_trace_listener)
        try:
            with log_context(
                run_id=ts.run_id,
                agent=getattr(self.agent, "_agent_name", "") or "agent",
            ):
                self._emit(
                    "run_resumed",
                    {
                        "resume_status": "step-resumable",
                        "step_index": checkpoint.get("step_index", 0),
                        "tool": checkpoint.get("tool", ""),
                    },
                )
                self._step_guard.reset(
                    task_summary=self._get_task_summary_text(),
                    suspect_files=self._extract_suspect_files(),
                    localization_mode=(getattr(self.agent, "_agent_name", "") == "patcher"),
                )
                path = (
                    "native"
                    if checkpoint.get("path") == "native"
                    and hasattr(self.agent.model_client, "complete_turn")
                    else "xml"
                )
                answer = self._run_loop(user_message, ts, callback, path=path)
                self._run_memory_dream()
                return answer
        finally:
            cb.remove_listener(self._circuit_trace_listener)

    def _restore_runtime_control(self, control: dict) -> None:
        """Restore budget/deadline/retry counters without resetting run limits."""
        from agent_runtime.repair_runtime import ExecutionDeadline

        if not control:
            return
        max_steps = int(control.get("max_steps", self.max_steps) or self.max_steps)
        self.max_steps = max_steps
        budget = control.get("budget") or {}
        if budget:
            self._repair_budget.restore(budget)
        manager_snapshot = control.get("budget_manager")
        if manager_snapshot:
            self._budget_manager.restore(manager_snapshot)
            self.agent.session["runtime_budget"] = self._budget_manager.snapshot()
            self._budget_turns_seen = int((manager_snapshot.get("used") or {}).get("turns", 0) or 0)
        deadline = control.get("deadline") or {}
        self._repair_deadline = ExecutionDeadline.from_remaining(deadline.get("remaining_s"))
        self.agent._repair_deadline = self._repair_deadline
        self._protocol_state.retry_count = int(
            control.get("retry_count", self._protocol_state.retry_count) or 0
        )
        self._tool_state.no_progress_steps = int(control.get("no_progress_steps", 0) or 0)
        self._protocol_state.json_retry_count = int(control.get("json_retry_count", 0) or 0)
        self._protocol_state.empty_retries = int(control.get("empty_retries", 0) or 0)

    def _begin_native_turn(self, ts, callback) -> None:
        from agent_runtime.turn_progress import TurnEventEmitter

        self._close_turn_progress()
        self._turn_progress = TurnEventEmitter(
            str(getattr(self.agent, "shared_run_id", "") or ts.run_id),
            "turn-" + uuid.uuid4().hex,
            self._append_turn_progress,
            lambda event: self._deliver_turn_progress(event, callback),
        )
        self._turn_progress.emit("turn_started", status="running")
        self.agent._turn_event_emitter = self._turn_progress

    def _run_loop(self, user_message: str, ts, callback=None, *, path=None) -> str:
        """One control loop; protocol adapters return tools, finals or recoverable errors."""
        from agent_runtime.parse_recovery import make_parse_retry

        path = path or ("native" if hasattr(self.agent.model_client, "complete_turn") else "xml")
        native = path == "native"
        native_tail: list[dict] = []
        native_tail_refs: dict[str, str] = {}
        usage_total = dict.fromkeys(
            (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "calls",
            ),
            0,
        )
        output_recovery = 0
        output_recovery_directive = ""
        turn = 0
        while True:
            turn += 1
            recovery_turn = native and turn > self.max_steps
            if native:
                if turn > self.max_steps + self._max_native_recovery_turns:
                    ts.stop_step_limit(self.max_steps)
                    return self._complete_run(
                        ts,
                        f"<final>已达到最大工具调用步数限制({self.max_steps})，当前任务未完成。</final>",
                    )
                self._begin_native_turn(ts, callback)
                if recovery_turn and not (
                    self._tool_state.patch_recovery_directive
                    or self._tool_state.patch_decision_required
                ):
                    ts.stop_step_limit(self.max_steps)
                    return self._complete_run(
                        ts, f"<final>已达到最大推理轮数限制({self.max_steps})。</final>"
                    )
            if (msg := self._check_run_deadline(ts)) is not None:
                return msg
            if not native:
                if (msg := self._abort_if_cancelled(ts, phase="step_start")) is not None:
                    return msg
                if (msg := self._check_xml_loop_limits(ts)) is not None:
                    return msg
            # Text format retries retain the tool-step index; native requests count turns.
            step = turn if native else ts.tool_steps + 1
            latency_decision = self._apply_latency_decision(
                int(getattr(self.agent.config, "max_new_tokens", 0) or 4096)
            )
            if native and not recovery_turn and not self._repair_budget.allow_turn():
                ts.stop_step_limit(self.max_steps)
                return self._complete_run(
                    ts, f"<final>已达到最大推理轮数限制({self.max_steps})。</final>"
                )
            if not recovery_turn and not self._budget_reserve_turn(step):
                ts.stop_with_reason(
                    StopReason.BUDGET_EXHAUSTED, "stopped", detail="unified budget turns exhausted"
                )
                return self._complete_run(ts, "<final>统一推理轮次预算已耗尽。</final>")
            if native:
                if not recovery_turn:
                    self._repair_budget.record_turn()
                if (msg := self._abort_if_cancelled(ts, phase="native_reasoning")) is not None:
                    return msg
                self._begin_edit_lock_turn()
            step_clock = StepClock(self._step_timeout_limit_s())
            if (msg := self._maybe_step_timeout(ts, step_clock, step, path)) is not None:
                return msg
            if native:
                self._notify_react_phase(
                    ReactPhase.REASONING, step=step, path=path, callback=callback
                )
            self._notify("on_step_start", callback, step=step, max_steps=self.max_steps, path=path)
            try:
                if native:
                    response = native_model_turn(
                        self._protocol_runtime(),
                        ts,
                        user_message,
                        turn=step,
                        step_clock=step_clock,
                        latency_decision=latency_decision,
                        native_tail=native_tail,
                        native_tail_refs=native_tail_refs,
                        usage_total=usage_total,
                        output_recovery_directive=output_recovery_directive,
                        output_recovery=output_recovery,
                        callback=callback,
                    )
                else:
                    response = xml_model_turn(
                        self._protocol_runtime(),
                        ts,
                        user_message,
                        step=step,
                        step_clock=step_clock,
                        callback=callback,
                    )
            except ContextBuildBlockedError as e:
                return self._context_blocked(ts, e)
            except ContextTooLargeError as e:
                if native:
                    self._emit(
                        "context_build_failed",
                        {"step": step, "actual": e.actual, "limit": e.limit, **e.metadata},
                    )
                ts.stop_with_reason(
                    StopReason.CONTEXT_OVERFLOW,
                    "stopped",
                    detail=f"actual={e.actual} limit={e.limit}",
                )
                return self._complete_run(ts, e.user_message)
            except StepTimeoutError as e:
                return self._finish_step_timeout(ts, e, clock=step_clock)
            except CancelledError as e:
                return e.answer or self._finish_user_cancel(ts, phase="model_wait")
            except Exception as e:
                if native and isinstance(e, TimeoutError):
                    error = StepTimeoutError(self._step_timeout_limit_s(), step=step, path=path)
                    return self._finish_step_timeout(ts, error, clock=step_clock)
                if (msg := self._stop_for_api_error(ts, e)) is not None:
                    return msg
                if not native:
                    raise
                ts.stop_with_reason(StopReason.API_ERROR, "failed", detail=f"error: {e}")
                return self._complete_run(ts, f"<final>API 错误: {e}</final>")

            payload = response.payload
            if response.status == "stop" and response.response_kind == "final":
                return payload["text"]  # The adapter already finalized this failure.
            if response.response_kind == "error":
                if native:
                    output_recovery_directive = payload["directive"]
                    output_recovery += 1
                elif payload.get("error_code") == "invalid_tool_payload":
                    user_message = xml_invalid_tool_retry(
                        self._protocol_runtime(),
                        ts,
                        payload.get("value", {}),
                        raw=payload["raw"],
                        step=step,
                    )
                else:
                    user_message = handle_parse_retry(
                        self._protocol_runtime(),
                        ts,
                        payload["raw"],
                        make_parse_retry(payload["raw"]),
                        step=step,
                    )
                continue
            recording = {"step": step, "path": path, "callback": callback}
            if response.response_kind == "final":
                answer = str(payload.get("text", ""))
                validation_error = ""
                if native:
                    ok, error = validate_final_answer(self.agent.config, answer)
                    validation_error = error if not ok else ""
                else:
                    retry_message = xml_final_retry(
                        self._protocol_runtime(), ts, payload["raw"], answer, step=step
                    )
                    if retry_message is not None:
                        user_message = retry_message
                        continue
                return self._finish_answer(
                    ts, answer, recording=recording, validation_error=validation_error
                )
            if (msg := self._maybe_step_timeout(ts, step_clock, step, path)) is not None:
                return msg
            try:
                if native:
                    output_recovery_directive = ""
                    results, refs = self._run_native_batch(
                        ts,
                        payload["calls"],
                        turn=step,
                        callback=callback,
                        native_content=payload["content"],
                    )
                    native_tail_refs.update(refs)
                    native_tail.extend(
                        [
                            {"role": "assistant", "content": payload["assistant_content"]},
                            {"role": "user", "content": results},
                        ]
                    )
                else:
                    user_message = self._run_text_tool(ts, payload["call"], step, callback)
            except TerminalToolAcceptedError as e:
                return self._finish_terminal_tool(ts, e, recording=None if native else recording)
            except CancelledError as e:
                return e.answer or self._finish_user_cancel(
                    ts, phase="tool_batch" if native else "tool"
                )
            if not native and self.stop_reason:
                return self._complete_run(ts, user_message, recording=recording)

    def _run_text_tool(self, ts, call, step, callback):
        from agent_runtime.repair_runtime import CanonicalToolCall, ToolSource

        call = CanonicalToolCall.create(call.name, call.arguments, source=ToolSource.TEXT)
        self.agent.session["_last_canonical_tool_call"] = {
            "call_id": call.call_id,
            "name": call.name,
            "arguments": call.arguments,
            "source": call.source.value,
        }
        self.agent.session["_pending_canonical_tool_call"] = {
            "call_id": call.call_id,
            "source": call.source.value,
        }
        return self._run_tool_step(
            ts, call.name, call.arguments, step=step, path="xml", callback=callback
        )

    def _merge_budget_meta(self, meta: dict) -> None:
        budget_meta = self._protocol_state.last_budget_meta or {}
        if not budget_meta:
            return
        sections = dict(meta.get("sections") or {})
        for key, value in budget_meta.get("sections", {}).items():
            sections[f"budget_{key}"] = value
        meta["sections"] = sections
        meta["budget"] = budget_meta.get("budget", meta.get("budget"))
        meta["prompt_budget"] = budget_meta.get("prompt_budget")
        cuts = list(meta.get("cuts") or [])
        cuts.extend(budget_meta.get("cuts") or [])
        if cuts:
            meta["cuts"] = cuts
        if not meta.get("total_tokens"):
            meta["total_tokens"] = budget_meta.get("total_tokens", 0)
        meta["runtime_budget"] = self._budget_payload()

    def _apply_call_usage_meta(self, call_usage: dict) -> None:
        inp = int(call_usage.get("input_tokens", 0) or 0)
        out = int(call_usage.get("output_tokens", 0) or 0)
        self._protocol_state.last_token_meta = {
            "total_tokens": inp + out,
            "input_tokens": inp,
            "output_tokens": out,
            "api_calls": int(call_usage.get("calls", 0) or 0),
            "sections": {"api_input": inp, "api_output": out},
            "source": "api_usage",
        }
        self._merge_budget_meta(self._protocol_state.last_token_meta)

    def _gen_task_summary(self, user_message: str):
        from agent_runtime.features.memory import set_task_summary

        client = getattr(self.agent, "light_client", None)
        if client is None:
            set_task_summary(self.agent.session["memory"], user_message)
            return

        try:
            raw = client.complete(
                f"Summarize this task in one short sentence (max 20 words):\n{user_message[:500]}",
                max_new_tokens=2048,
            )
            summary = raw.strip()[:300] if raw else user_message[:300]
        except Exception:
            summary = user_message[:300]

        set_task_summary(self.agent.session["memory"], summary)

    def _get_task_summary_text(self) -> str:
        """从 session memory 读取当前任务摘要。"""
        mem = self.agent.session.get("memory", {})
        working = mem.get("working", {})
        return working.get("task_summary", "") or ""

    def _extract_suspect_files(self) -> set[str]:
        """从 plan_todos + task_summary + traceback 中提取 suspect 文件名。

        E10: 仅保留能映射到 workspace 内的路径作 goal_drift 锚点；
        仓外 repro（如 temp/save_ps.py）不得作为唯一目标。
        """
        import re
        from pathlib import Path

        from agent_runtime.intent.stack_parse import relativize_suspect_path

        files: set[str] = set()
        # 1. 从 plan_todos 提取（如 content 含文件名）
        for todo in self._plan_todos:
            content = todo.get("content", "")
            for m in re.findall(r"[\w/\-]+\.py", content):
                files.add(m.split("/")[-1].split("\\")[-1])

        # 2. 从 task_summary 提取
        task = self._get_task_summary_text()
        for m in re.findall(r"[\w/\-]+\.py", task):
            files.add(m.split("/")[-1].split("\\")[-1])

        # 3. 从 traceback 提取全部 File "..." 帧（非仅首帧）
        user_req = self._task_state.user_request if self._task_state else ""
        for raw in re.findall(r'File\s+"([^"]+)"', user_req):
            files.add(raw.split("/")[-1].split("\\")[-1])

        root = None
        try:
            root = Path(self.agent.tool_context.root)
        except (AttributeError, TypeError):
            root = None
        if root is None or not root.is_dir():
            return files

        in_repo: set[str] = set()
        for raw in re.findall(r'File\s+"([^"]+)"', user_req):
            mapped = relativize_suspect_path(raw, repo_root=root)
            if mapped:
                in_repo.add(Path(mapped).name)
        for name in list(files):
            if (root / name).is_file():
                in_repo.add(name)
            else:
                mapped = relativize_suspect_path(name, repo_root=root)
                if mapped:
                    in_repo.add(Path(mapped).name)
        # 空集合 → StepGuard 跳过 drift（避免仓外锚点误杀）
        return in_repo

    def _run_memory_dream(self) -> None:
        """Agent ask() 结束后执行 Memory Dream：去重+过期+裁剪+晋升建议+路由重建。"""
        mem = self.agent.session.get("memory")
        if not mem:
            return
        from agent_runtime.features.memory.dream import dream_summary_to_trace, run_memory_dream

        root = getattr(self.agent, "_cwd", "") or ""
        stats, dreamer = run_memory_dream(mem, durable_root=root)
        if any(
            stats.get(k, 0)
            for k in (
                "deduped",
                "expired",
                "trimmed",
                "durable_gc",
                "promotion_suggestions",
            )
        ):
            self._emit("memory_dream", dream_summary_to_trace(stats, dreamer))
        # 存储 dream stats 供 _build_memory_health 合并
        self._last_dream_stats = stats

    def _get_store(self):
        if self._store is None:
            from agent_runtime.run_store import RunStore

            self._store = RunStore(
                root=self.agent._cwd,
                state_root=str(getattr(self.agent.tool_context, "state_root", "") or ""),
            )
        return self._store

    def _begin_edit_lock_turn(self) -> None:
        """Phase B：新推理 turn 重置写串行计数。"""
        try:
            from src.repair.execution.edit_lock import get_active_edit_lock

            root = getattr(getattr(self.agent, "tool_context", None), "root", None) or getattr(
                self.agent, "_cwd", None
            )
            lock = get_active_edit_lock(root)
            if lock is not None and hasattr(lock, "begin_turn"):
                lock.begin_turn()
        except Exception:
            pass

    def _record_tool_outcome(
        self, tool_name: str, result, ts, tool_args: dict | None = None
    ) -> None:
        from agent_runtime.tool_result import attach_tool_receipt

        call = self.agent.session.get("_last_canonical_tool_call", {}) or {}
        normalized = attach_tool_receipt(
            result,
            tool_name,
            args_hash=str(call.get("arguments_hash", "") or ""),
            run_id=str(getattr(ts, "run_id", "") or ""),
            call_id=str(call.get("call_id", "") or ""),
        )
        ts.record_tool_rejection(tool_name, normalized.metadata)
        self._emit_tool_trace(tool_name, normalized, tool_args)

    def _emit_tool_trace(self, tool_name: str, result, tool_args: dict | None = None) -> None:
        from agent_runtime.tool_rejection import tool_trace_payload

        meta = getattr(result, "metadata", None) or {}
        tier = meta.get("execution_tier", "host")
        self._tier_counts[tier] = self._tier_counts.get(tier, 0) + 1
        self._tier_tools.setdefault(tier, {})[tool_name] = (
            self._tier_tools[tier].get(tool_name, 0) + 1
        )
        try:
            from agent_runtime.metrics import get_registry

            get_registry().counter_inc("fixloop_tool_steps_total", labels={"tier": tier})
        except Exception:
            pass
        preview = meta.get("patch_preview")
        if preview:
            self._emit("tool_preview", {"tool": tool_name, **preview})
        content = str(getattr(result, "content", "") or "")
        trace_payload = tool_trace_payload(
            tool_name,
            meta,
            tool_args=tool_args,
            result_content=content,
        )
        if meta.get("provider") == "mcp":
            self._emit("mcp_call", trace_payload)
        self._emit("tool_executed", trace_payload)

    def _emit(self, event: str, payload: dict | None = None):
        try:
            from agent_runtime.l2_context import l2_payload_from_agent, l2_payload_from_task_state

            payload = dict(payload or {})
            agent_name = getattr(self.agent, "_agent_name", "") or "agent"
            payload.setdefault("agent", agent_name)
            ts = self._task_state
            l2_extra = l2_payload_from_task_state(ts) or l2_payload_from_agent(self.agent)
            for key, value in l2_extra.items():
                payload.setdefault(key, value)
            shared = getattr(self.agent, "shared_run_id", None)
            run_id = shared or (ts.run_id if ts else "")
            if run_id:
                payload.setdefault("run_id", run_id)
            store = self._get_store()
            if shared:
                store.append_trace_event(shared, event, payload)
            elif self._task_state:
                store.append_trace(self._task_state, event, payload)
        except Exception:
            pass

    def _finalize_run(self, ts):
        self._close_turn_progress(
            status=("uncertain" if str(ts.status) == "running" else str(ts.status))
        )
        from agent_runtime import loop_finalizer

        try:
            loop_finalizer.finalize_agent_run(self, ts)
        finally:
            service = self.agent.tool_context.exploration_service
            if service is not None:
                service.close()
                self.agent.tool_context.exploration_service = None
