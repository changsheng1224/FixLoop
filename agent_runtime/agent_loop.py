"""Agent 控制循环：感知 → 决策 → 行动 → 记录 → 循环。

停机后产出 task_state.json + trace.jsonl + report.json（含 node_timings 耗时分布）。
"""

import json
import time as _time
import uuid

from agent_runtime.cancellation import CancelledError, run_with_cancellation
from agent_runtime.compression_pipeline import truncate_tool_result_for_agent
from agent_runtime.context_metadata import build_trace_payload
from agent_runtime.errors import ContextBuildBlockedError, ContextTooLargeError, EmptyModelResponse
from agent_runtime.loop_limits import max_parse_attempts
from agent_runtime.model_timing import (
    ModelCallTiming,
    collect_client_timings,
    emit_model_timing_events,
)
from agent_runtime.parse_recovery import (
    ParseRetry,
    build_recovery_prompt,
    failure_invalid_tool_payload,
)
from agent_runtime.providers.retry_policy import RateLimitExceededError
from agent_runtime.react_phases import ReactPath, ReactPhase
from agent_runtime.step_clock import StepClock, StepTimeoutError
from agent_runtime.step_guard import StepContext, StepGuard
from agent_runtime.stop_reasons import StopReason
from agent_runtime.terminal_tool import TerminalToolAcceptedError

# StepGuard stall 检测：仅"可能修改文件"的工具才计入停滞
_MODIFYING_TOOLS = frozenset({"write_file", "patch_file", "apply_patch", "run_shell"})


def _tool_target_paths(tool_name: str, tool_args: dict | None) -> list[str]:
    """Return normalized file targets for write/recovery decisions.

    ``apply_patch`` carries its paths in the patch envelope rather than a
    top-level ``path`` argument.  Recovery must use those exact paths so a
    stale write cannot fall back to a wildcard read reservation.
    """
    args = tool_args or {}
    direct = str(args.get("path") or "").replace("\\", "/").strip()
    if tool_name != "apply_patch":
        return [direct] if direct else []

    patch_text = args.get("patch") or args.get("diff") or args.get("input") or ""
    if not str(patch_text).strip():
        return []
    try:
        from agent_runtime.apply_patch_format import parse_apply_patch_text

        ops = parse_apply_patch_text(str(patch_text))
    except (TypeError, ValueError):
        return []
    paths: list[str] = []
    for op in ops:
        path = str(getattr(op, "path", "") or "").replace("\\", "/").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _log_loop(msg: str) -> None:
    """Loop 阶段 debug 日志（受 --log-level 控制）。"""
    from agent_runtime.logging_setup import get_logger

    get_logger("agent_loop").debug(msg.rstrip("\n"))


def _patch_recovery_anchors(text: str, *, max_chars: int = 6000) -> str:
    """Keep bounded source/target anchors when a model turn is truncated."""
    raw = str(text or "")
    if not raw:
        return ""
    lines = raw.splitlines()
    markers = (
        "allowed_edit:",
        "DISK GROUNDING",
        "嫌疑位置",
        "相关测试文件",
        "失败面",
        "[PATCHER RUNTIME CONTRACT]",
    )
    starts = [
        index for index, line in enumerate(lines) if any(marker in line for marker in markers)
    ]
    chunks: list[str] = []
    used = 0
    for start in starts:
        chunk = "\n".join(lines[start : start + 36]).strip()
        if not chunk:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining].rstrip() + "\n... anchors truncated ..."
        chunks.append(chunk)
        used += len(chunk)
    return "\n\n".join(chunks)


def _build_anthropic_tools(
    tools_registry: dict, *, allowed_names: set[str] | None = None
) -> list[dict]:
    """将内部工具注册表转换为 Anthropic tool_use 格式（仅 schema 字段）。"""
    from agent_runtime.tool_schema import schema_to_json, tool_schema_view

    result = []
    for name, spec in tool_schema_view(tools_registry).items():
        if allowed_names is not None and name not in allowed_names:
            continue
        schema = spec.get("json_schema") or spec.get("schema", {})
        result.append(
            {
                "name": name,
                "description": spec.get("description", ""),
                "input_schema": schema_to_json(schema),
            }
        )
    return result


class AgentLoop:
    """Agent 控制循环。管理对话回合，统计步数，产出 trace 工件。"""

    def __init__(self, agent, max_steps: int | None = None, *, stream: bool = False):
        self.agent = agent
        self._observation_recorder = self._new_observation_recorder()
        agent._loop = self
        self.max_steps = max_steps or agent.config.max_steps
        self.stop_reason = ""
        self._task_state = None
        self._store = None
        self._last_token_meta = {}
        self._retry_count = 0
        self._stream_enabled = stream
        self._call_timings: list[ModelCallTiming] = []
        self._in_flight_tool = ""
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
        self._no_progress_steps = 0
        self._step_guard = StepGuard()
        self._json_retry_count = 0
        self._empty_retries = 0
        self.MAX_EMPTY_RETRIES = 3
        self._last_dream_stats: dict[str, int] = {}
        self._llm_call_count = 0
        self._blocked_convergence_reads = 0
        self._patch_decision_required = False
        # Patcher recovery is explicit state, so a rejected write/read cannot
        # silently fall back to the previous model/tool projection.
        self._patch_recovery_directive = ""
        self._patch_recovery_allowed_tools: set[str] | None = None
        self._patch_recovery_kind = ""
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
        inflight = in_flight or self._in_flight_tool
        return self._finish_user_cancel(ts, phase=phase, in_flight=inflight)

    def _finish_user_cancel(self, ts, *, phase: str, in_flight: str = "") -> str:
        inflight = in_flight or self._in_flight_tool
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
        self._patch_recovery_kind = str(kind)
        self._patch_recovery_directive = str(directive)
        self._patch_recovery_allowed_tools = set(allowed_tools)
        self._patch_decision_required = True

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
                state.node_timings["allowed_edit"] = sorted(lock.allowed_edit)
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
            if self._patch_recovery_allowed_tools is not None:
                # Recovery directives are stricter than the normal convergence
                # window.  In particular stale writes expose only the exact
                # reread, while malformed writes expose apply_patch.
                return set(self._patch_recovery_allowed_tools)
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
            self._step_guard.targeted_read_available and not self._patch_decision_required
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

    def _run_tool_step(self, ts, tool_name, tool_args, **kwargs):
        flow = self._tool_step_flow(ts, tool_name, tool_args, **kwargs)
        try:
            request = next(flow)
        except StopIteration as done:
            return done.value
        try:
            context = kwargs.get("call_context")
            if request is not None:
                result = request
            elif context is not None:
                result = self.agent.execute_tool(tool_name, tool_args, call_context=context)
            else:
                result = self.agent.execute_tool(tool_name, tool_args)
        except BaseException as exc:
            flow.throw(exc)
            raise
        try:
            flow.send(result)
        except StopIteration as done:
            return done.value
        raise RuntimeError("tool step yielded twice")

    def _tool_step_flow(
        self,
        ts,
        tool_name: str,
        tool_args: dict,
        *,
        step: int,
        path: ReactPath,
        callback=None,
        record_assistant: bool = True,
        emit_acting: bool = True,
        emit_observation: bool = True,
        emit_recording: bool = True,
        call_context=None,
        prepared_result=None,
    ):
        """执行工具、写 trace/history，返回下一轮 user_message。"""
        if (
            prepared_result is None
            and (msg := self._abort_if_cancelled(ts, phase="pre_tool", in_flight=tool_name))
            is not None
        ):
            raise CancelledError("user", answer=msg)
        from agent_runtime.tool_budget import infer_tool_budget_group

        shared_inflight = call_context is None or not call_context.isolated
        tool_registry = call_context.registry if call_context is not None else self.agent.tools

        group = infer_tool_budget_group(tool_name, (tool_registry or {}).get(tool_name))
        idempotency_key = (
            call_context.idempotency_key
            if call_context is not None
            else f"{ts.run_id}:{step}:{tool_name}"
        )
        replayed = prepared_result is not None
        if prepared_result is not None:
            result = prepared_result
        replay_blocked = False
        from agent_runtime.context_runtime import find_action_by_idempotency

        prior_action = (
            None
            if prepared_result is not None
            else find_action_by_idempotency(self.agent.session, idempotency_key)
        )
        if prior_action is not None:
            prior_status = str(prior_action.get("status", ""))
            prior_spec = (tool_registry or {}).get(tool_name) or {}
            if prior_status in {"verified", "succeeded"}:
                from agent_runtime.tool_executor import ToolExecutionResult

                result = ToolExecutionResult(
                    content="[idempotent replay] 已复用已验证的工具结果",
                    status="success",
                    metadata={
                        "tool_status": "success",
                        "replayed": True,
                        "observation_id": prior_action.get("result_ref", ""),
                        "receipt": prior_action.get("receipt", {}),
                    },
                )
                replayed = True
            elif prior_status in {"dispatched", "uncertain"} and str(
                prior_spec.get("side_effect", "none")
            ) not in {"read", "none", ""}:
                from agent_runtime.tool_executor import ToolExecutionResult

                result = ToolExecutionResult(
                    content="Error: 幂等操作状态不确定，需先执行 postcondition reconciliation",
                    status="uncertain",
                    error_code="idempotency_conflict",
                    metadata={
                        "tool_status": "uncertain",
                        "tool_error_code": "idempotency_conflict",
                        "retryable": False,
                        "action": prior_action,
                    },
                )
                replay_blocked = True
        is_patcher = (
            getattr(self.agent, "agent_name", None) or getattr(self.agent, "_agent_name", "") or ""
        ) == "patcher"
        convergence_blocked = False
        if is_patcher and not replayed and not replay_blocked:
            quota_summary = (
                self.agent.quota.quota_summary()
                if hasattr(getattr(self.agent, "quota", None), "quota_summary")
                else {}
            )
            read_budget = (quota_summary.get("groups") or {}).get("read") or {}
            remaining = read_budget.get("remaining")
            if remaining is not None and int(remaining) <= 2:
                if self._step_guard.enter_convergence("read_budget_low"):
                    self._enter_convergence_gate("read_budget_low", step=step)
            phase_before = self._step_guard.phase
            read_reservation = self._matching_read_reservation(tool_name, tool_args)
            preflight = self._step_guard.preflight(
                tool_name,
                tool_args,
                read_reservation=read_reservation,
            )
            if phase_before == "explore" and self._step_guard.phase == "converge":
                self._enter_convergence_gate(
                    self._step_guard.convergence_reason or "duplicate_read", step=step
                )
            if preflight is not None and preflight.action == "allow_targeted_read":
                self._grant_read_reserve("*", kind="targeted", step=step)
            elif preflight is not None and preflight.action == "allow_reserved_read":
                pass
            elif preflight is not None and preflight.action.startswith("block_"):
                from agent_runtime.tool_executor import ToolExecutionResult

                event = (
                    "duplicate_read_blocked"
                    if preflight.action == "block_duplicate_read"
                    else "convergence_read_blocked"
                )
                self._emit(
                    event,
                    {
                        "step": step,
                        "tool": tool_name,
                        "path": str(tool_args.get("path") or ""),
                        "phase": self._step_guard.phase,
                    },
                )
                self._blocked_convergence_reads += 1
                if self._blocked_convergence_reads >= 2:
                    self._patch_decision_required = True
                    self._emit(
                        "patch_decision_required",
                        {
                            "step": step,
                            "blocked_read_attempts": self._blocked_convergence_reads,
                            "allowed_actions": [
                                "apply_patch",
                                "patch_file",
                                "expand_lock",
                                "terminal",
                            ],
                        },
                    )
                result = ToolExecutionResult(
                    content=(
                        f"Error: {preflight.detail}。{preflight.replan_hint} "
                        "可用动作: apply_patch/patch_file/expand_lock/终止。"
                    ),
                    status="rejected",
                    error_code="convergence_required",
                    metadata={
                        "tool_status": "rejected",
                        "tool_error_code": "convergence_required",
                        "retryable": False,
                    },
                )
                self._set_patch_recovery(
                    "convergence_required",
                    "读取请求被收敛闸门拒绝。请停止重复读取，直接调用 apply_patch/patch_file，"
                    "或调用 finish_repair 说明证据不足。",
                    {"apply_patch", "patch_file", "finish_repair"},
                )
                convergence_blocked = True
        budget_rejected = self._repair_deadline.expired()
        if not replayed and not replay_blocked and not convergence_blocked and budget_rejected:
            from agent_runtime.tool_executor import ToolExecutionResult

            result = ToolExecutionResult(
                content="Error: repair 全局执行期限已耗尽",
                metadata={
                    "tool_status": "rejected",
                    "tool_error_code": "deadline_exceeded",
                    "retryable": False,
                },
            )
        elif (
            not replayed
            and not replay_blocked
            and not convergence_blocked
            and (
                not self._repair_budget.allow_tool(group.value)
                or (
                    self._repair_budget.max_tool_calls > 0
                    and self._repair_budget.tool_calls + getattr(self, "_pending_batch_tools", 0)
                    >= self._repair_budget.max_tool_calls
                )
                or not self._budget_allows_tool(group.value)
            )
        ):
            from agent_runtime.tool_executor import ToolExecutionResult

            result = ToolExecutionResult(
                content=f"Error: 工具组 {group.value} 预算已耗尽",
                metadata={
                    "tool_status": "rejected",
                    "tool_error_code": "budget_exceeded",
                    "retryable": False,
                    "budget_group": group.value,
                },
            )
            budget_rejected = True
        self._notify(
            "on_pre_tool",
            callback,
            step=step,
            tool_name=tool_name,
            tool_args=tool_args,
            path=str(path),
        )
        if emit_acting:
            self._notify_react_phase(
                ReactPhase.ACTING,
                step=step,
                path=path,
                tool=tool_name,
                callback=callback,
            )
        if record_assistant:
            self.agent.record(
                {
                    "role": "assistant",
                    "content": f"调用工具: {tool_name}",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                }
            )
        t0 = _time.time()
        if not budget_rejected and not replayed and not replay_blocked and not convergence_blocked:
            self._budget_reserve("tool_calls")
            if group.value in {"write", "verify", "recovery"}:
                self._budget_reserve(
                    {"write": "writes", "verify": "verifies", "recovery": "recoveries"}[group.value]
                )
            if call_context is not None:
                call_context.budget_reserved = True
            if shared_inflight:
                self._in_flight_tool = tool_name
            else:
                self._pending_batch_tools = getattr(self, "_pending_batch_tools", 0) + 1
            from agent_runtime.context_runtime import build_action_record, transition_action

            tool_spec = (tool_registry or {}).get(tool_name) or {}
            action = build_action_record(
                tool_name,
                tool_args,
                revision=int(self.agent.session.get("state_revision", 0) or 0),
                side_effect=str(tool_spec.get("side_effect", "none") or "none"),
                idempotency_key=idempotency_key,
                status="dispatched",
            )
            action_raw = action.__dict__.copy()
            if shared_inflight:
                self.agent.session["_in_flight_action"] = action_raw
            try:
                result = yield
                self._block_grounded_finish(tool_name, tool_args, result)
            except BaseException:
                action_raw["status"] = "uncertain"
                action_raw["uncertain_reason"] = "runtime_exception"
                if call_context is not None:
                    self.agent.session.setdefault("action_ledger", []).append(action_raw)
                raise
            else:
                result_meta = getattr(result, "metadata", {}) or {}
                result_status = str(result_meta.get("tool_status", "error"))
                error_code = str(result_meta.get("tool_error_code", "") or "")
                if result_status == "success":
                    next_status = (
                        "verified"
                        if str(tool_spec.get("side_effect", "none")) in {"read", "none"}
                        or result_meta.get("postcondition_verified")
                        else "acknowledged"
                    )
                elif result_status in {"cancelled", "uncertain"} or error_code in {
                    "tool_timeout",
                    "deadline_exceeded",
                    "tool_cancelled",
                }:
                    next_status = "uncertain"
                else:
                    next_status = "failed"
                try:
                    action_raw = transition_action(
                        action_raw,
                        next_status,
                        reason=error_code,
                        receipt=result_meta.get("receipt"),
                    )
                except ValueError:
                    action_raw["status"] = "uncertain"
                    action_raw["uncertain_reason"] = "invalid_transition"
                action_raw["result_ref"] = str(result_meta.get("observation_id", ""))
                self.agent.session.setdefault("action_ledger", []).append(action_raw)
                self.agent.session["action_ledger"] = self.agent.session["action_ledger"][-100:]
                if shared_inflight:
                    self.agent.session.pop("_in_flight_action", None)
            finally:
                if shared_inflight:
                    self._in_flight_tool = ""
                else:
                    self._pending_batch_tools -= 1
        if budget_rejected or replayed or replay_blocked or convergence_blocked:
            result = yield result
        if call_context is not None:
            from agent_runtime.tool_executor import _canonical_args_hash
            from agent_runtime.tool_result import attach_tool_receipt

            result = attach_tool_receipt(
                result,
                tool_name,
                args_hash=_canonical_args_hash(tool_name, tool_args),
                run_id=call_context.run_id,
                call_id=call_context.call_id,
            )
        # Gateway/权限拒绝不计入 tool_steps，避免无效步耗尽预算（E5）
        _meta = getattr(result, "metadata", None) or {}
        if _meta.get("tool_status") != "rejected" and prepared_result is None:
            ts.record_tool(tool_name)
            self._repair_budget.record_tool(group.value)
        else:
            ts.last_tool = tool_name
        if (
            call_context is None
            and (msg := self._abort_if_cancelled(ts, phase="post_tool", in_flight=tool_name))
            is not None
        ):
            raise CancelledError("user", answer=msg)
        result_text = result.content if hasattr(result, "content") else str(result)
        result_meta = getattr(result, "metadata", {}) or {}
        error_code = str(result_meta.get("tool_error_code", "") or "")
        if is_patcher:
            target_paths = _tool_target_paths(tool_name, tool_args)
            path_hint = target_paths[0] if target_paths else ""
            if error_code == "stale_preimage":
                if self._step_guard.request_targeted_reread("stale_preimage"):
                    for target_path in target_paths:
                        self._grant_read_reserve(target_path, kind="targeted", step=step)
                self._set_patch_recovery(
                    "stale_preimage",
                    f"补丁的旧文本已失效。先对 {', '.join(target_paths) or '目标文件'} "
                    "执行一次精确 read_file，"
                    "再基于刚读到的上下文调用 apply_patch/patch_file；不要重复旧补丁。",
                    {"read_file", "finish_repair"}
                    if self._has_targeted_read_reserve()
                    else {"apply_patch", "finish_repair"},
                )
                self._emit(
                    "stale_patch_rejected",
                    {
                        "step": step,
                        "tool": tool_name,
                        "path": path_hint,
                        "paths": target_paths,
                        "current_sha256": result_meta.get("current_sha256", ""),
                        "recovery_action": "targeted_reread_then_retry",
                    },
                )
            elif error_code == "invalid_args":
                self._set_patch_recovery(
                    "invalid_args",
                    "写入参数无效。禁止空 old_text/new_text 或重复相同工具调用；"
                    "请改用包含文件路径、上下文行和 +/- 行的 apply_patch，"
                    "或调用 finish_repair 说明无法修复。",
                    {"apply_patch", "finish_repair"},
                )
                self._emit(
                    "patch_write_rejected",
                    {"step": step, "tool": tool_name, "error_code": error_code},
                )
            elif error_code == "no_change":
                self._step_guard.request_targeted_reread("no_change")
                for target_path in target_paths:
                    self._grant_read_reserve(target_path, kind="targeted", step=step)
                self._set_patch_recovery(
                    "no_change",
                    f"上一次写入没有产生磁盘变化。先精确读取 "
                    f"{', '.join(target_paths) or '目标文件'} 的当前内容，"
                    "再提交不同的 apply_patch，或调用 finish_repair。",
                    {"read_file", "finish_repair"},
                )
                ts.node_timings["patch_no_change"] = True
                self._emit(
                    "patch_no_change",
                    {
                        "step": step,
                        "tool": tool_name,
                        "recovery_action": "reread_then_retry_or_finish",
                    },
                )
            elif error_code == "edit_lint_reject":
                self._set_patch_recovery(
                    "edit_lint_reject",
                    "补丁因编辑期语法检查未落盘。请修正语法后用 apply_patch 提交，"
                    "不要重复相同内容。",
                    {"apply_patch", "finish_repair"},
                )
                self._emit(
                    "patch_write_rejected",
                    {"step": step, "tool": tool_name, "error_code": error_code},
                )
        te_ms = int((_time.time() - t0) * 1000)
        from agent_runtime.repair_runtime import CanonicalToolCall

        raw_call = self.agent.session.get("_last_canonical_tool_call", {})
        canonical_call = CanonicalToolCall.create(
            tool_name,
            tool_args,
            source=raw_call.get("source", "native"),
            call_id=(
                call_context.call_id if call_context is not None else raw_call.get("call_id", "")
            ),
        )
        if (
            call_context is not None
            and call_context.isolated
            and tool_name == "read_file"
            and result.ok
        ):
            from agent_runtime.tools import _mark_edit_lock_read

            _mark_edit_lock_read(self.agent.tool_context, str(tool_args.get("path", "")))
        recorded = self._observation_recorder.record(
            canonical_call,
            result,
            duration_ms=te_ms,
            metadata=_meta,
            source_version=((tool_registry or {}).get(tool_name) or {}).get("version", ""),
            idempotency_key=idempotency_key,
            call_context=call_context,
        )
        stored = recorded.stored
        self._last_tool_observation_id = stored.observation_id
        retrieval = _meta.get("retrieval_result")
        # 权限拒绝：立即回灌，避免反复试 run_shell/sandbox_test
        if _meta.get("rejection_reason") == "role_not_allowed" or (
            _meta.get("tool_status") == "rejected" and tool_name in ("run_shell", "sandbox_test")
        ):
            if (
                getattr(self.agent, "agent_name", None)
                or getattr(self.agent, "_agent_name", "")
                or ""
            ) == "patcher":
                result_text = (
                    f"{result_text}\n"
                    "【停】patcher 无权限调用此工具。请改用：read_file → apply_patch "
                    "→ quick_test；需要扩锁时用 expand_lock。"
                )
        self._notify(
            "on_post_tool",
            callback,
            step=step,
            tool_name=tool_name,
            result_preview=result_text[:200],
            elapsed_ms=te_ms,
            path=str(path),
        )
        projection_input = result_text
        if isinstance(retrieval, dict):
            from agent_runtime.code_exploration.consumption import retrieval_header

            projection_input = retrieval_header(retrieval) + result_text
        projected_result_text = truncate_tool_result_for_agent(
            self.agent, tool_name, projection_input
        )
        if len(projected_result_text) < len(projection_input):
            _meta["output_truncated"] = True
            if hasattr(result, "output_truncated"):
                result.output_truncated = True
            if stored.raw_ref:
                projected_result_text += (
                    f"\n[output_truncated=true artifact_ref={stored.raw_ref} "
                    f"observation_id={stored.observation_id}]"
                )
        result_text = projected_result_text
        _meta["duration_ms"] = te_ms
        ts.node_timings.setdefault("tool_exec_ms", 0)
        ts.node_timings["tool_exec_ms"] += te_ms
        _log_loop(f"  [loop] {tool_name} tool={te_ms}ms\n")
        if emit_observation:
            self._notify_react_phase(
                ReactPhase.OBSERVATION,
                step=step,
                path=path,
                tool=tool_name,
                callback=callback,
            )
        if _meta.get("tool_status") == "success":
            self.agent.update_memory_after_tool(tool_name, tool_args, result_text)
            self._sync_patcher_grounding(tool_name, tool_args, result)
        self._record_tool_outcome(tool_name, result, ts, tool_args)
        if emit_recording:
            self._notify_react_phase(
                ReactPhase.RECORDING,
                step=step,
                path=path,
                tool=tool_name,
                callback=callback,
            )
        # 确定工具执行状态
        tool_status = "OK"
        if result.metadata.get("tool_status") != "success":
            if "Error" in result_text:
                tool_status = "FAIL"
            elif "[DRY RUN]" in result_text:
                tool_status = "DRY"
        self._notify(
            "on_tool_executed",
            callback,
            step=step,
            name=tool_name,
            result_preview=result_text,
            elapsed_ms=te_ms,
            status=tool_status,
        )
        # 终态工具：成功后结束 loop，payload 作为 final answer
        tool_spec = (tool_registry or {}).get(tool_name) or {}
        if (
            tool_spec.get("terminal")
            and result.metadata.get("tool_status") == "success"
            and not str(result_text).startswith("Error")
        ):
            from agent_runtime.terminal_tool import TerminalToolAcceptedError

            raise TerminalToolAcceptedError(str(result_text), tool_name=tool_name)
        # 死循环检测：Gate 5.5 rejection → 升级为 stop
        error_code = result.metadata.get("tool_error_code", "")
        if error_code == "loop_detected":
            from agent_runtime.tool_executor import _canonical_args_hash

            self._emit(
                "loop_detected",
                {
                    "tool": tool_name,
                    "args_hash": _canonical_args_hash(tool_name, tool_args),
                    "window_size": int(getattr(self.agent.config, "loop_detect_threshold", 3) or 3),
                },
            )
            ts.stop_with_reason(
                StopReason.CIRCUIT_BREAKER,
                "stopped",
                detail=f"死循环检测: {tool_name} 连续高频调用",
            )
            self.stop_reason = StopReason.CIRCUIT_BREAKER
            return ts.final_answer or f"任务因死循环检测终止（{tool_name}）。"

        # 每 tool 步 checkpoint（成功时），供 --resume 从最后成功步继续
        tool_success = result.metadata.get("tool_status") == "success"
        if tool_success:
            self._advance_todo()
            if tool_name in {"write_file", "patch_file", "apply_patch"}:
                self._patch_decision_required = False
                self._patch_recovery_directive = ""
                self._patch_recovery_allowed_tools = None
                self._patch_recovery_kind = ""
            consumed_reserve = result.metadata.get("read_reserve_consumed")
            if isinstance(consumed_reserve, dict):
                self._emit(
                    "post_lock_read_consumed"
                    if consumed_reserve.get("kind") == "post_lock"
                    else "targeted_read_consumed",
                    {"step": step, **consumed_reserve},
                )
                if (
                    tool_name == "read_file"
                    and consumed_reserve.get("kind") == "targeted"
                    and self._patch_recovery_kind in {"stale_preimage", "no_change"}
                ):
                    self._set_patch_recovery(
                        "post_reread",
                        "精确重读已完成。现在必须基于该读取结果调用 apply_patch/patch_file，"
                        "或调用 finish_repair；不要再次读取同一范围。",
                        {"apply_patch", "patch_file", "finish_repair"},
                    )
            if (
                tool_name == "expand_lock"
                and tool_args.get("path")
                and "expanded:" in str(result_text)
            ):
                generation = 0
                try:
                    from src.repair.execution.edit_lock import get_active_edit_lock

                    root = getattr(getattr(self.agent, "tool_context", None), "root", None)
                    lock = get_active_edit_lock(root)
                    if lock is not None:
                        generation = lock.required_read_generation(str(tool_args["path"]))
                except Exception:
                    generation = 0
                self._grant_read_reserve(
                    str(tool_args["path"]).replace("\\", "/"),
                    kind="post_lock",
                    step=step,
                    generation=generation,
                )

        # StepGuard：仅「改盘」算进展。patcher 的 read/grep 不再伪装成 has_affected，
        # 否则会空转耗尽 step_limit（R11 django）。
        meta = result.metadata if hasattr(result, "metadata") else {}
        affected = meta.get("affected_paths", []) if isinstance(meta, dict) else []
        self._no_progress_steps = self._step_guard.stall_count
        if is_patcher:
            guard_has_affected = bool(affected) or (
                tool_name in _MODIFYING_TOOLS
                and meta.get("tool_status") == "success"
                and not str(result_text).startswith("Error")
            )
        else:
            guard_has_affected = bool(affected) or tool_name not in _MODIFYING_TOOLS
        verdict = None
        if not convergence_blocked:
            verdict = self._step_guard.evaluate(
                StepContext(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    has_affected=guard_has_affected,
                    progress_key=(
                        self._step_guard.read_progress_key(tool_name, tool_args)
                        if tool_success
                        else ""
                    ),
                )
            )
        if verdict is not None:
            if verdict.reason:
                # 终止级判决
                self._emit(
                    "stall_detected" if verdict.reason == StopReason.STALL else "goal_drift",
                    {
                        "reason": verdict.reason,
                        "detail": verdict.detail,
                        "steps": self._step_guard.stall_count,
                        "drift_steps": self._step_guard.drift_count,
                    },
                )
                for todo in self._plan_todos:
                    if todo.get("status") == "in_progress":
                        todo["status"] = "blocked"
                        self._emit("todo_updated", {"todo": dict(todo)})
                        break
                # stall 不终止：注入 replan 提示让模型自行调整
                if verdict.reason == StopReason.STALL:
                    hint = (
                        f"\n\n⚠ 进展停滞（连续 {self._step_guard.stall_count} 步无文件变更）。"
                        "请检查当前 todo 列表，考虑重新规划或尝试不同策略。"
                    )
                    if is_patcher:
                        hint += (
                            " 【patcher】下一步必须对实现文件 apply_patch（含 - 上下文）；"
                            "不要继续纯 read/grep；不要用 run_shell/sandbox_test。"
                        )
                    result_text = result_text + hint
                    self.agent.record(
                        {
                            "role": "tool",
                            "content": (f"[{stored.observation_id}] {result_text[:800]}"),
                            "tool_name": tool_name,
                            "observation_id": stored.observation_id,
                        }
                    )
                    next_message = f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"
                    self._persist_step_checkpoint(
                        ts,
                        tool_name,
                        tool_args,
                        result_text,
                        next_message,
                        result,
                        step=step,
                        path=path,
                    )
                    return next_message
                # goal_drift 仍终止
                ts.stop_with_reason(verdict.reason, "stopped", detail=verdict.detail)
                self.stop_reason = verdict.reason
                return verdict.replan_hint or f"任务终止：{verdict.detail}"
            else:
                if verdict.action == "enter_convergence":
                    self._enter_convergence_gate(
                        self._step_guard.convergence_reason or "read_limit_without_write",
                        step=step,
                    )
                    result_text += f"\n\n{verdict.replan_hint}"
                else:
                    # warning 级（drift 预警，不终止）
                    self._emit("goal_drift_warning", {"detail": verdict.detail})
        self.agent.record(
            {
                "role": "tool",
                "content": f"[{stored.observation_id}] {result_text[:800]}",
                "tool_name": tool_name,
                "observation_id": stored.observation_id,
            }
        )
        next_message = f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"
        progress = getattr(self, "_turn_progress", None)
        if progress is not None:
            self.agent.session["turn_progress"] = progress.checkpoint(
                getattr(self, "_active_tool_batch", None)
            )
        self._persist_step_checkpoint(
            ts,
            tool_name,
            tool_args,
            result_text,
            next_message,
            result,
            step=step,
            path=path,
        )
        return next_message

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
                return SettledToolStep(done.value, self._last_tool_observation_id)
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

    def _check_hard_cap(self, token_meta: dict) -> str | None:
        """Prompt 超出 configured hard cap 时返回错误消息。"""
        total = token_meta.get("total_tokens", 0) or token_meta.get("context_sections_total", 0)
        hard_cap = int(getattr(self.agent.config, "hard_cap", 8_000) or 8_000)
        if total > hard_cap:
            return (
                f"<final>Prompt 大小 {total} tokens 超出硬顶限制 ({hard_cap})。"
                "请缩短输入或使用 /reset 清空对话历史后重试。</final>"
            )
        return None

    def _context_blocked(self, ts, error: ContextBuildBlockedError) -> str:
        self._emit("context_blocked", {"reason": error.reason, **error.metadata})
        self.agent.session["context_blocked"] = {"reason": error.reason, **error.metadata}
        ts.stop_with_reason(StopReason.CONTEXT_BLOCKED, "stopped", detail=error.reason)
        return self._complete_run(ts, error.user_message)

    def _xml_build_context(self, ts, user_message: str, *, step: int, callback) -> str:
        t0 = _time.time()
        # XML continuations (including old checkpoints) carry raw tool output.
        # Reference code observations instead, so the current history projection
        # is the only source body used after freshness checks.
        last = self.agent.session.get("_last_tool_observation", {})
        oid = str(last.get("observation_id", ""))
        record = self.agent.session.get("observations", {}).get(oid, {})
        prefix = f"工具 {record.get('tool', '')} 执行完成。\n结果:\n"
        if record.get("retrieval_result") and user_message.startswith(prefix):
            user_message = prefix + f"[observation_ref={oid}; see validated context history]"
        from agent_runtime.context_manager import ContextManager

        manager = ContextManager(self.agent)
        request, token_meta = manager.prepare_request(user_message, protocol="xml")
        prompt_text = request.messages[0]["content"]
        self._prepared_context_manager = manager
        self._prepared_request = request
        if hard_limit := self._check_hard_cap(token_meta):
            return hard_limit
        if token_meta.get("required_state_ref"):
            try:
                self.agent._plan_session.validate_required_context(token_meta["long_task_context"])
            except ValueError:
                raise ContextBuildBlockedError("state_mismatch") from None
        from agent_runtime.message_projection import (
            attach_projection_metadata,
            build_context_prefix,
        )

        context_prefix = build_context_prefix(self.agent, token_meta)
        attach_projection_metadata(token_meta, self.agent.session, context_prefix=context_prefix)
        self._last_token_meta = token_meta
        if not self._budget_reserve("prompt_tokens", token_meta.get("total_tokens", 0)):
            return "<final>Prompt token 预算已耗尽。</final>"
        self._accumulate_context_stats(token_meta)
        self._emit("context_built", build_trace_payload(token_meta))
        self._begin_edit_lock_turn()
        self._notify_react_phase(
            ReactPhase.REASONING,
            step=step,
            path="xml",
            callback=callback,
        )
        ts.node_timings.setdefault("prompt_build_ms", 0)
        ts.node_timings["prompt_build_ms"] += int((_time.time() - t0) * 1000)
        return prompt_text

    def _xml_call_model(
        self, ts, prompt_text: str, *, step: int, callback=None
    ) -> tuple[str, float]:
        # LLM 调用预算硬顶
        max_calls = getattr(self.agent.config, "max_llm_calls_per_repair", 0) or 0
        if max_calls > 0 and self._llm_call_count >= max_calls:
            ts.stop_with_reason(
                StopReason.BUDGET_EXHAUSTED, "stopped", detail=f"max_llm_calls={max_calls}"
            )
            self.stop_reason = StopReason.BUDGET_EXHAUSTED
            raise CancelledError(
                "budget", answer=(f"<final>LLM 调用达到硬顶 ({max_calls})，任务终止。</final>")
            )
        if not self._budget_reserve("llm_calls"):
            ts.stop_with_reason(
                StopReason.BUDGET_EXHAUSTED,
                "stopped",
                detail="unified budget llm_calls exhausted",
            )
            self.stop_reason = StopReason.BUDGET_EXHAUSTED
            raise CancelledError("budget", answer="<final>统一预算已耗尽，任务终止。</final>")
        self._llm_call_count += 1
        ts.record_attempt()
        t1 = _time.time()
        self._emit(
            "model_request_start",
            {
                "step": ts.tool_steps + 1,
                "attempt": ts.attempts,
                "model": getattr(self.agent.config, "model", ""),
                "runtime_budget": self._budget_payload(),
            },
        )
        meta = getattr(self, "_last_token_meta", None) or {}
        cache_key = str(meta.get("prompt_cache_key", "") or "")
        effective_output_tokens = int(
            (self.agent.session.get("runtime_degradation") or {}).get(
                "max_output_tokens",
                getattr(self.agent.config, "max_new_tokens", 4096),
            )
            or 4096
        )
        for empty_try in range(self.MAX_EMPTY_RETRIES):
            try:
                if meta.get("request_hash"):
                    self._prepared_context_manager.validate_prepared_request(
                        self._prepared_request, meta
                    )
                if self._stream_enabled and hasattr(self.agent.model_client, "complete_stream"):

                    def on_chunk(chunk: str) -> None:
                        self._emit_stream_event(
                            "token_delta", {"chars": len(chunk)}, phase="model", turn=step
                        )
                        if callback is not None and hasattr(callback, "on_chunk"):
                            callback.on_chunk(chunk)

                    raw = self._invoke_model_call(
                        lambda: self.agent.circuit_breaker.call(
                            self.agent.model_client.complete_stream,
                            prompt_text,
                            max_new_tokens=effective_output_tokens,
                            on_chunk=on_chunk,
                            cancel_token=self._cancel_token,
                        )
                    )
                else:
                    try:
                        raw = self._invoke_model_call(
                            lambda: self.agent.circuit_breaker.call(
                                self.agent.model_client.complete,
                                prompt_text,
                                max_new_tokens=effective_output_tokens,
                                prompt_cache_key=cache_key,
                            )
                        )
                    except TypeError as exc:
                        if "prompt_cache_key" not in str(exc):
                            raise
                        raw = self._invoke_model_call(
                            lambda: self.agent.circuit_breaker.call(
                                self.agent.model_client.complete,
                                prompt_text,
                                max_new_tokens=effective_output_tokens,
                            )
                        )
                break  # 成功，退出重试循环
            except EmptyModelResponse:
                self._empty_retries += 1
                self._emit(
                    "empty_model_response",
                    {
                        "attempt": empty_try + 1,
                        "step": step,
                    },
                )
                if empty_try < self.MAX_EMPTY_RETRIES - 1:
                    if not self._sleep_with_deadline(0.5 * (empty_try + 1)):
                        raise CancelledError(
                            "deadline",
                            answer="<final>重试退避期间已达到全局 deadline。</final>",
                        )
                else:
                    ts.stop_with_reason(
                        StopReason.API_ERROR,
                        "stopped",
                        detail="empty_model_response_exhausted",
                    )
                    self.stop_reason = StopReason.API_ERROR
                    raise CancelledError(
                        "api_error",
                        answer=(
                            "<final>API 错误: 模型连续返回空响应，已重试 "
                            f"{self.MAX_EMPTY_RETRIES} 次。</final>"
                        ),
                    )

        ts.node_timings.setdefault("model_call_ms", 0)
        ts.node_timings["model_call_ms"] += int((_time.time() - t1) * 1000)
        self._record_model_timings(
            ts,
            collect_client_timings(self.agent.model_client),
            default_attempt=ts.attempts,
        )
        return raw, t1

    def _handle_parse_retry(self, ts, raw: str, payload, *, step: int) -> str:
        self._retry_count += 1
        delay = min(2 ** (self._retry_count - 1), 8)
        _log_loop(
            f"  [loop] retry#{self._retry_count} backoff={delay}s raw[:100]={raw.strip()[:100]}\n"
        )
        try:
            from pathlib import Path

            dbg = Path(self.agent._cwd) / ".agent" / "debug_retry.txt"
            dbg.parent.mkdir(parents=True, exist_ok=True)
            with open(dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== retry#{self._retry_count} ===\n{raw}\n")
        except Exception:
            pass
        if not self._sleep_with_deadline(delay):
            raise CancelledError(
                "deadline",
                answer="<final>解析重试退避期间已达到全局 deadline。</final>",
            )
        prompt = str(payload)
        failure = payload.failure if isinstance(payload, ParseRetry) else None
        if failure is not None:
            self._emit(
                "parse_retry",
                {
                    "kind": failure.kind,
                    "attempt": self._retry_count,
                    "snippet_len": len(failure.snippet),
                    "error_offset": failure.error_offset,
                },
            )
        self.agent.record({"role": "system", "content": prompt})
        return prompt

    def _xml_invalid_tool_retry(self, ts, payload, *, raw: str, step: int) -> str:
        failure = failure_invalid_tool_payload(payload)
        last = self._last_successful_tool_call()
        prompt = build_recovery_prompt(failure, last_tool_call=last)
        retry = ParseRetry(prompt, failure, has_last_tool_anchor=last is not None)
        return self._handle_parse_retry(ts, raw, retry, step=step)

    def _last_successful_tool_call(self) -> dict | None:
        """从 session history 中找上一次成功的 tool 调用。"""
        history = self.agent.session.get("history", [])
        for h in reversed(history):
            if h.get("role") == "tool" and h.get("tool_name"):
                return {"name": h["tool_name"], "args": h.get("tool_args", {})}
        return None

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
        self._retry_count = 0
        self._blocked_convergence_reads = 0
        self._patch_decision_required = False
        self._patch_recovery_directive = ""
        self._patch_recovery_allowed_tools = None
        self._patch_recovery_kind = ""
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
                self._no_progress_steps = 0
                # 重置 StepGuard + 注入任务上下文
                self._step_guard.reset(
                    task_summary=self._get_task_summary_text(),
                    suspect_files=self._extract_suspect_files(),
                    localization_mode=(agent_name == "patcher"),
                )

                if hasattr(self.agent.model_client, "complete_turn"):
                    answer = self._run_with_native_tools(user_message, ts, callback)
                else:
                    answer = self._run_with_text_parsing(user_message, ts, callback)
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
        """从最后一个成功 tool step 后继续 XML ReAct 循环。"""
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
                if checkpoint.get("path") == "native" and hasattr(
                    self.agent.model_client, "complete_turn"
                ):
                    answer = self._run_with_native_tools(user_message, ts, callback)
                else:
                    answer = self._run_with_text_parsing(user_message, ts, callback)
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
        self._retry_count = int(control.get("retry_count", self._retry_count) or 0)
        self._no_progress_steps = int(control.get("no_progress_steps", 0) or 0)
        self._json_retry_count = int(control.get("json_retry_count", 0) or 0)
        self._empty_retries = int(control.get("empty_retries", 0) or 0)

    def _run_with_native_tools(self, user_message: str, ts, callback=None) -> str:
        client = self.agent.model_client
        from agent_runtime.message_projection import (
            attach_projection_metadata,
            build_context_prefix,
        )
        from agent_runtime.model_turn import (
            FinishKind,
            ToolChoice,
            ToolChoiceMode,
        )

        native_tail: list[dict] = []
        native_tail_refs: dict[str, str] = {}
        usage_total = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "calls": 0,
        }
        output_recovery = 0
        output_recovery_directive = ""
        started = _time.time()

        # A rejected gateway/executor call needs one bounded recovery request;
        # it must not consume the normal repair-turn budget.  Extra turns are
        # available only while an explicit patcher recovery directive exists.
        for turn in range(1, self.max_steps + self._max_native_recovery_turns + 1):
            self._close_turn_progress()
            import uuid

            from agent_runtime.turn_progress import TurnEventEmitter

            self._turn_progress = TurnEventEmitter(
                str(getattr(self.agent, "shared_run_id", "") or ts.run_id),
                "turn-" + uuid.uuid4().hex,
                self._append_turn_progress,
                lambda event: self._deliver_turn_progress(event, callback),
            )
            self._turn_progress.emit("turn_started", status="running")
            self.agent._turn_event_emitter = self._turn_progress
            recovery_turn = turn > self.max_steps
            if recovery_turn and not (
                self._patch_recovery_directive or self._patch_decision_required
            ):
                ts.stop_step_limit(self.max_steps)
                return self._complete_run(
                    ts,
                    f"<final>已达到最大推理轮数限制({self.max_steps})。</final>",
                )
            if self._repair_deadline.expired():
                ts.stop_with_reason(
                    StopReason.DEADLINE_EXCEEDED,
                    "stopped",
                    detail="repair wall-clock deadline exceeded",
                )
                return self._complete_run(
                    ts,
                    "<final>已达到 repair 全局执行期限，当前任务停止。</final>",
                )
            configured_output = int(getattr(self.agent.config, "max_new_tokens", 0) or 4096)
            latency_decision = self._apply_latency_decision(configured_output)
            if not recovery_turn and not self._repair_budget.allow_turn():
                ts.stop_step_limit(self.max_steps)
                return self._complete_run(
                    ts,
                    f"<final>已达到最大推理轮数限制({self.max_steps})。</final>",
                )
            if not recovery_turn and not self._budget_reserve_turn(turn):
                ts.stop_with_reason(
                    StopReason.BUDGET_EXHAUSTED,
                    "stopped",
                    detail="unified budget turns exhausted",
                )
                return self._complete_run(ts, "<final>统一推理轮次预算已耗尽。</final>")
            if not recovery_turn:
                self._repair_budget.record_turn()
            if (msg := self._abort_if_cancelled(ts, phase="native_reasoning")) is not None:
                return msg
            self._begin_edit_lock_turn()
            step_clock = StepClock(self._step_timeout_limit_s())
            if (msg := self._maybe_step_timeout(ts, step_clock, turn, "native")) is not None:
                return msg
            self._notify_react_phase(
                ReactPhase.REASONING, step=turn, path="native", callback=callback
            )
            self._notify(
                "on_step_start",
                callback,
                step=turn,
                max_steps=self.max_steps,
                path="native",
            )
            from agent_runtime.context_manager import ContextManager

            action_required = bool(output_recovery_directive or self._patch_decision_required)
            tools_def = _build_anthropic_tools(
                self.agent.tools,
                allowed_names=self._native_tool_names(
                    action_required=action_required,
                    patch_only_recovery=bool(output_recovery_directive),
                ),
            )
            phase_output_cap = 4096 if not action_required or output_recovery_directive else 2048
            max_output = max(
                512, min(latency_decision["max_output_tokens"], phase_output_cap, 8192)
            )
            directives = []
            user_override = None
            if output_recovery_directive:
                if getattr(self.agent, "_plan_session", None) is not None:
                    directives.append(output_recovery_directive)
                else:
                    # Preserve the established standalone L1 recovery envelope.
                    summary = (
                        self._get_task_summary_text().strip() or ts.user_request or user_message
                    )
                    user_override = output_recovery_directive
                    if summary:
                        user_override += f"\n[REPAIR TASK]\n{summary[:2000]}"
                    anchors = _patch_recovery_anchors(user_message)
                    if anchors:
                        user_override += f"\n[PATCHER EVIDENCE ANCHORS]\n{anchors}"
            elif self._patch_decision_required:
                directives.append(
                    "[PATCH DECISION REQUIRED] Exploration is closed. "
                    "Call apply_patch/patch_file now, or call finish_repair with a concise "
                    "cannot_patch/needs_more_context reason grounded in the evidence ledger."
                )
            if self._patch_recovery_directive:
                directives.append("[PATCH RECOVERY]\n" + self._patch_recovery_directive)
            if user_override and directives:
                user_override += "\n\n" + "\n\n".join(directives)
                directives = []
            manager = ContextManager(self.agent)
            try:
                request, budget_meta = manager.prepare_request(
                    user_message,
                    protocol="native",
                    tools=tools_def,
                    native_tail=native_tail,
                    tail_refs=native_tail_refs,
                    directives=directives,
                    action_required=action_required,
                    tool_choice=ToolChoice(ToolChoiceMode.REQUIRED) if action_required else None,
                    max_output_tokens=max_output,
                    deadline=getattr(step_clock, "_deadline", None),
                    user_override=user_override,
                )
            except ContextBuildBlockedError as e:
                return self._context_blocked(ts, e)
            except ContextTooLargeError as e:
                self._emit(
                    "context_build_failed",
                    {"step": turn, "actual": e.actual, "limit": e.limit, **e.metadata},
                )
                ts.stop_with_reason(
                    StopReason.CONTEXT_OVERFLOW,
                    "stopped",
                    detail=f"actual={e.actual} limit={e.limit}",
                )
                return self._complete_run(ts, e.user_message)
            dynamic_user = request.messages[0]["content"]
            self._prepared_context_manager = manager
            self._prepared_request = request
            context_prefix = build_context_prefix(self.agent, budget_meta)
            attach_projection_metadata(
                budget_meta, self.agent.session, context_prefix=context_prefix
            )
            self._last_budget_meta = budget_meta
            budget_meta["runtime_budget"] = self._budget_payload()
            if not self._budget_reserve("prompt_tokens", budget_meta["provider_input_tokens"]):
                ts.stop_with_reason(
                    StopReason.CONTEXT_OVERFLOW, "stopped", detail="prompt token budget exhausted"
                )
                return self._complete_run(ts, "<final>Prompt token 预算已耗尽。</final>")
            self._accumulate_context_stats(budget_meta)
            self._emit("context_built", build_trace_payload(budget_meta))
            if isinstance(budget_meta.get("emergency_compaction"), dict):
                self._emit(
                    "context_emergency_compacted",
                    {"step": turn, **budget_meta["emergency_compaction"]},
                )
            if not self._budget_reserve("llm_calls"):
                ts.stop_with_reason(
                    StopReason.BUDGET_EXHAUSTED,
                    "stopped",
                    detail="unified budget llm_calls exhausted",
                )
                return self._complete_run(ts, "<final>统一 LLM 调用预算已耗尽。</final>")
            self._emit(
                "model_request_start",
                {
                    "step": turn,
                    "attempt": turn,
                    "model": getattr(self.agent.config, "model", ""),
                    "runtime_budget": self._budget_payload(),
                },
            )
            self._notify(
                "on_pre_model",
                callback,
                step=turn,
                prompt_preview=dynamic_user[:200],
                path="native",
            )
            call_started = _time.time()
            try:
                manager.validate_prepared_request(request, budget_meta)
                result = self._invoke_model_call(lambda: client.complete_turn(request))
                step_clock.check(step=turn, path="native")
            except ContextBuildBlockedError as e:
                return self._context_blocked(ts, e)
            except StepTimeoutError as e:
                return self._finish_step_timeout(ts, e, clock=step_clock)
            except TimeoutError:
                error = StepTimeoutError(self._step_timeout_limit_s(), step=turn, path="native")
                return self._finish_step_timeout(ts, error, clock=step_clock)
            except CancelledError as e:
                if e.answer:
                    return e.answer
                return self._finish_user_cancel(ts, phase="model_wait")
            except Exception as e:
                if (msg := self._stop_for_api_error(ts, e)) is not None:
                    return msg
                ts.stop_with_reason(StopReason.API_ERROR, "failed", detail=f"error: {e}")
                return self._complete_run(ts, f"<final>API 错误: {e}</final>")

            ts.record_attempt()
            elapsed_ms = int((_time.time() - call_started) * 1000)
            for key in usage_total:
                if key == "calls":
                    continue
                usage_total[key] += int(result.usage.get(key, 0) or 0)
            usage_total["calls"] += 1
            self._apply_call_usage_meta(usage_total)
            self._record_model_timings(ts, collect_client_timings(client), default_attempt=turn)
            ts.node_timings["model_call_ms"] = (
                int(ts.node_timings.get("model_call_ms", 0) or 0) + elapsed_ms
            )
            finish = result.finish
            ts.node_timings["provider_finish_kind"] = finish.kind.value
            ts.node_timings["provider_finish_reason"] = finish.raw_reason
            self._emit(
                "provider_finish",
                {
                    "step": turn,
                    "kind": finish.kind.value,
                    "raw_reason": finish.raw_reason,
                    "provider": finish.provider,
                },
            )
            self._notify(
                "on_post_model",
                callback,
                step=turn,
                raw_preview=result.text[:200],
                elapsed_ms=elapsed_ms,
                path="native",
            )

            if finish.kind not in {FinishKind.MAX_OUTPUT_TOKENS, FinishKind.EMPTY_OUTPUT}:
                output_recovery_directive = ""

            if finish.kind == FinishKind.TOOL_CALLS and result.tool_calls:
                assistant_content = result.content or [
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in result.tool_calls
                ]
                tool_results: list[dict] = []
                try:
                    tool_results, batch_refs = self._run_native_batch(
                        ts,
                        result.tool_calls,
                        turn=turn,
                        callback=callback,
                        native_content=result.content,
                    )
                    native_tail_refs.update(batch_refs)
                except TerminalToolAcceptedError as e:
                    self._emit(
                        "terminal_tool_accepted",
                        {"tool": e.tool_name, "payload_chars": len(e.payload)},
                    )
                    self.agent.record({"role": "assistant", "content": e.payload})
                    ts.finish_success(e.payload)
                    return self._complete_run(ts, e.payload)
                except CancelledError as exc:
                    return exc.answer or self._finish_user_cancel(ts, phase="tool_batch")
                native_tail.extend(
                    [
                        {"role": "assistant", "content": assistant_content},
                        {"role": "user", "content": tool_results},
                    ]
                )
                continue

            if finish.kind == FinishKind.MAX_OUTPUT_TOKENS:
                content_blocks = result.content if isinstance(result.content, list) else []
                block_counts: dict[str, int] = {}
                for block in content_blocks:
                    block_type = (
                        str(block.get("type") or "unknown")
                        if isinstance(block, dict)
                        else "invalid"
                    )
                    block_counts[block_type] = block_counts.get(block_type, 0) + 1
                thinking_only = (
                    bool(content_blocks)
                    and set(block_counts) <= {"thinking"}
                    and not result.text
                    and not result.tool_calls
                )
                if thinking_only:
                    self._patch_decision_required = True
                    self._step_guard.enter_convergence("thinking_only_truncation")
                    self._emit(
                        "thinking_only_truncation",
                        {
                            "step": turn,
                            "requested_max_output_tokens": max_output,
                            "actual_output_tokens": int(result.usage.get("output_tokens", 0) or 0),
                            "recovery_attempt": output_recovery + 1,
                        },
                    )
                self._emit(
                    "model_output_truncated",
                    {
                        "step": turn,
                        "requested_max_output_tokens": max_output,
                        "actual_output_tokens": int(result.usage.get("output_tokens", 0) or 0),
                        "text_chars": len(result.text or ""),
                        "content_block_count": len(content_blocks),
                        "content_block_counts": block_counts,
                        "tool_call_count": len(result.tool_calls),
                        "recovery_attempt": output_recovery + 1,
                        "history_action": "discarded",
                    },
                )
                if output_recovery < 1:
                    output_recovery += 1
                    output_recovery_directive = (
                        "[OUTPUT RECOVERY] The previous model output was truncated and was "
                        "discarded. Do not continue or repeat that analysis. Complete this turn "
                        "with exactly one apply_patch/patch_file call, or call finish_repair with "
                        "a grounded cannot_patch/needs_more_context reason. "
                        "Do not perform more broad exploration."
                    )
                    continue
                ts.stop_with_reason(
                    StopReason.MODEL_OUTPUT_TRUNCATED,
                    "failed",
                    detail=(
                        f"provider_finish={finish.kind.value}; "
                        f"requested={max_output}; recovery_attempts={output_recovery}"
                    ),
                )
                return self._complete_run(
                    ts,
                    '<final>{"status":"needs_more_context",'
                    '"reason":"模型输出连续被截断，未执行不完整内容。"}</final>',
                )

            if finish.kind == FinishKind.EMPTY_OUTPUT:
                if output_recovery < 1:
                    output_recovery += 1
                    output_recovery_directive = (
                        "[EMPTY OUTPUT RECOVERY] The previous response was empty. Produce exactly "
                        "one apply_patch/patch_file call, or call finish_repair with a grounded "
                        "cannot_patch/needs_more_context reason."
                    )
                    continue
                ts.stop_with_reason(
                    StopReason.PARSE_FAIL,
                    "failed",
                    detail=f"provider_finish={finish.kind.value}",
                )
                return self._complete_run(ts, f"<final>模型输出无效：{finish.kind.value}</final>")

            if finish.kind == FinishKind.CONTENT_FILTER:
                ts.stop_with_reason(
                    StopReason.API_ERROR,
                    "failed",
                    detail=f"content_filter:{finish.raw_reason}",
                )
                return self._complete_run(ts, "<final>模型输出被 Provider 安全策略拦截。</final>")

            answer = result.text.strip()
            self.agent.record({"role": "assistant", "content": answer})
            ok, err_msg = self._validate_final_answer(answer)
            if not ok:
                self._emit("json_validation_warning", {"error": err_msg})
            if not self.stop_reason:
                ts.finish_success(answer)
            _log_loop(f"  [loop] final ({int((_time.time() - started) * 1000)}ms total)\n")
            return self._complete_run(
                ts,
                answer,
                recording={"step": turn, "path": "native", "callback": callback},
            )

        ts.stop_step_limit(self.max_steps)
        return self._complete_run(
            ts,
            f"<final>已达到最大工具调用步数限制({self.max_steps})，当前任务未完成。</final>",
        )

    def _run_with_text_parsing(self, user_message: str, ts, callback=None) -> str:
        while True:
            if self._repair_deadline.expired():
                ts.stop_with_reason(
                    StopReason.DEADLINE_EXCEEDED,
                    "stopped",
                    detail="repair wall-clock deadline exceeded",
                )
                return self._complete_run(
                    ts,
                    "<final>已达到 repair 全局执行期限，当前任务停止。</final>",
                )
            if (msg := self._abort_if_cancelled(ts, phase="step_start")) is not None:
                return msg
            if (msg := self._check_xml_loop_limits(ts)) is not None:
                return msg

            step = ts.tool_steps + 1
            self._apply_latency_decision(
                int(getattr(self.agent.config, "max_new_tokens", 0) or 4096)
            )
            if not self._budget_reserve_turn(step):
                ts.stop_with_reason(
                    StopReason.BUDGET_EXHAUSTED,
                    "stopped",
                    detail="unified budget turns exhausted",
                )
                return self._complete_run(ts, "<final>统一推理轮次预算已耗尽。</final>")
            step_clock = StepClock(self._step_timeout_limit_s())
            if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                return msg
            if callback is not None:
                self._notify(
                    "on_step_start",
                    callback,
                    step=step,
                    max_steps=self.max_steps,
                    path="xml",
                )

            try:
                prompt_text = self._xml_build_context(
                    ts, user_message, step=step, callback=callback
                )
            except ContextBuildBlockedError as e:
                return self._context_blocked(ts, e)
            except ContextTooLargeError as e:
                ts.stop_with_reason(
                    StopReason.CONTEXT_OVERFLOW,
                    "stopped",
                    detail=f"actual={e.actual} limit={e.limit}",
                )
                return self._complete_run(ts, e.user_message)
            # _check_hard_cap 返回 <final> 字符串时直接终止（不发给模型）
            if prompt_text.startswith("<final>"):
                ts.stop_with_reason(
                    StopReason.CONTEXT_OVERFLOW,
                    "stopped",
                    detail="hard_cap via legacy _check_hard_cap",
                )
                return self._complete_run(ts, prompt_text)

            self._notify(
                "on_pre_model",
                callback,
                step=step,
                prompt_preview=prompt_text[:200],
                path="xml",
            )
            try:
                raw, t1 = self._xml_call_model(ts, prompt_text, step=step, callback=callback)
            except ContextBuildBlockedError as e:
                return self._context_blocked(ts, e)
            except CancelledError as e:
                if e.answer:
                    return e.answer
                return self._finish_user_cancel(ts, phase="model_wait")
            except Exception as e:
                if (msg := self._stop_for_api_error(ts, e)) is not None:
                    return msg
                raise

            # CoT 提取：剥离思考内容后再进 history
            raw = self._strip_cot(raw)

            if (msg := self._abort_if_cancelled(ts, phase="post_model")) is not None:
                return msg

            t_parse = int((_time.time() - t1) * 1000)
            self._notify(
                "on_post_model",
                callback,
                step=step,
                raw_preview=raw[:200],
                elapsed_ms=t_parse,
                path="xml",
            )

            if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                return msg

            from agent_runtime.canonical_protocol import parse_model_response

            response = parse_model_response(raw, expected_tools=set(self.agent.tools))
            if response.response_kind == "final":
                kind, payload = "final", response.payload.get("text", "")
            elif response.response_kind == "tool_call":
                call = response.payload["call"]
                kind, payload = "tool", {"name": call.name, "args": call.arguments}
            else:
                from agent_runtime.parse_recovery import make_parse_retry

                if response.payload.get("error_code") == "invalid_tool_payload":
                    kind, payload = "tool", response.payload.get("value", {})
                else:
                    kind, payload = "retry", make_parse_retry(raw)

            if kind == "final":
                _log_loop(f"  [loop] final ({t_parse}ms parse)\n")
                final_text = str(payload)
                ok, err_msg = self._validate_final_answer(final_text)
                max_json_retries = getattr(
                    self.agent.config, "max_json_retries", self.MAX_JSON_RETRIES
                )
                if not ok and self._json_retry_count < max_json_retries:
                    self._json_retry_count += 1
                    self._emit(
                        "json_retry",
                        {
                            "attempt": self._json_retry_count,
                            "error": err_msg,
                        },
                    )
                    # 走 ParseRetry → _handle_parse_retry，回到 Acting
                    from agent_runtime.parse_recovery import (
                        ParseFailure,
                        ParseRetry,
                        build_recovery_prompt,
                    )

                    failure = ParseFailure(
                        kind="json_in_tool",
                        snippet=final_text[:500],
                        error_offset=None,
                        error_message=err_msg,
                        hint="final answer JSON 格式错误",
                    )
                    retry = ParseRetry(
                        build_recovery_prompt(failure),
                        failure,
                    )
                    user_message = self._handle_parse_retry(ts, raw, retry, step=step)
                    continue
                self._json_retry_count = 0
                self.agent.record({"role": "assistant", "content": final_text})
                ts.finish_success(final_text)
                return self._complete_run(
                    ts,
                    final_text,
                    recording={"step": step, "path": "xml", "callback": callback},
                )

            if kind == "tool":
                if not isinstance(payload, dict) or "name" not in payload:
                    user_message = self._xml_invalid_tool_retry(ts, payload, raw=raw, step=step)
                    continue
                if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                    return msg
                tool_name = payload.get("name", "unknown")
                tool_args = payload.get("args", {})
                from agent_runtime.repair_runtime import CanonicalToolCall, ToolSource

                canonical_call = CanonicalToolCall.create(
                    tool_name,
                    tool_args,
                    source=ToolSource.TEXT,
                )
                self.agent.session["_last_canonical_tool_call"] = {
                    "call_id": canonical_call.call_id,
                    "name": canonical_call.name,
                    "arguments": canonical_call.arguments,
                    "source": canonical_call.source.value,
                }
                self.agent.session["_pending_canonical_tool_call"] = {
                    "call_id": canonical_call.call_id,
                    "source": canonical_call.source.value,
                }
                try:
                    user_message = self._run_tool_step(
                        ts,
                        tool_name,
                        tool_args,
                        step=step,
                        path="xml",
                        callback=callback,
                    )
                except TerminalToolAcceptedError as e:
                    self._emit(
                        "terminal_tool_accepted",
                        {"tool": e.tool_name, "payload_chars": len(e.payload)},
                    )
                    self.agent.record({"role": "assistant", "content": e.payload})
                    ts.finish_success(e.payload)
                    return self._complete_run(
                        ts,
                        e.payload,
                        recording={"step": step, "path": "xml", "callback": callback},
                    )
                except CancelledError as e:
                    return e.answer
                if self.stop_reason:
                    # StepGuard 触发终止（stall / goal_drift）
                    return self._complete_run(
                        ts,
                        user_message,
                        recording={"step": step, "path": "xml", "callback": callback},
                    )
                continue

            if kind == "retry":
                if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                    return msg
                user_message = self._handle_parse_retry(ts, raw, payload, step=step)
                continue

    def _merge_budget_meta(self, meta: dict) -> None:
        budget_meta = getattr(self, "_last_budget_meta", None) or {}
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
        self._last_token_meta = {
            "total_tokens": inp + out,
            "input_tokens": inp,
            "output_tokens": out,
            "api_calls": int(call_usage.get("calls", 0) or 0),
            "sections": {"api_input": inp, "api_output": out},
            "source": "api_usage",
        }
        self._merge_budget_meta(self._last_token_meta)

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

    MAX_JSON_RETRIES = 2

    def _validate_final_answer(self, text: str) -> tuple[bool, str]:
        """校验 final answer 的 JSON 语法与可选 schema。

        Returns:
            (ok, error_message)。ok=True 表示通过，error_message 为 recovery 提示。
        """
        config = self.agent.config
        if not config.json_mode:
            return True, ""
        schema = config.final_schema

        # L1: JSON 语法
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return False, (
                f"上一轮 final answer 不是合法 JSON（{e}）。"
                "请严格输出 JSON 格式的最终答案，不要包裹在 markdown 代码块中。"
            )

        # L2: Schema 字段校验
        if schema:
            missing = [f for f in schema if f not in data]
            if missing:
                return False, (
                    f"上一轮 final answer 缺少必填字段: {missing}。"
                    f"请输出包含 {list(schema.keys())} 的完整 JSON。"
                )
            type_map = {
                "str": str,
                "int": int,
                "float": (int, float),
                "bool": bool,
                "list": list,
                "dict": dict,
            }
            for field, ftype in schema.items():
                expected = type_map.get(ftype)
                if expected is None:
                    continue
                value = data.get(field)
                if value is not None and not isinstance(value, expected):
                    return False, (
                        f"字段 '{field}' 应为 {ftype} 类型，实际为 {type(value).__name__}。"
                        "请修正后重新输出。"
                    )

        return True, ""

    @staticmethod
    def _strip_cot(raw: str) -> str:
        """剥离模型输出中的思考内容（CoT），返回清洗后的文本。

        两步：
        1. 移除 ``<think>...</think>`` 标签块（DeepSeek-R1 / OpenAI o1）
        2. 移除第一个 ``<tool>`` 或 ``<final>`` 标签前的自然语言前缀

        若清洗后为空，返回原始文本（安全回退）。
        """
        import re

        # Step 1: 移除显式 <think> 标签
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

        # Step 2: 移除第一个结构化标签前的自然语言前缀
        m = re.search(r"<(tool|final)>", cleaned)
        if m and m.start() > 0:
            prefix = cleaned[: m.start()].strip()
            if prefix:
                cleaned = cleaned[m.start() :]

        cleaned = cleaned.strip()
        return cleaned if cleaned else raw.strip()

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
