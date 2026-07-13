"""Agent 控制循环：感知 → 决策 → 行动 → 记录 → 循环。

停机后产出 task_state.json + trace.jsonl + report.json（含 node_timings 耗时分布）。
"""

import json
import time as _time

from agent_runtime.compression_pipeline import truncate_tool_result_for_agent
from agent_runtime.context_metadata import build_trace_payload
from agent_runtime.loop_limits import NATIVE_MAX_TURNS_MESSAGE, max_parse_attempts
from agent_runtime.model_timing import (
    ModelCallTiming,
    build_report_latency_fields,
    collect_client_timings,
    emit_model_timing_events,
)
from agent_runtime.parse_recovery import (
    ParseRetry,
    build_recovery_prompt,
    failure_invalid_tool_payload,
)
from agent_runtime.providers.retry_policy import RateLimitExceededError
from agent_runtime.react_phases import ReactPhase, ReactPath
from agent_runtime.step_clock import StepClock, StepTimeoutError
from agent_runtime.step_guard import StepContext, StepGuard
from agent_runtime.stop_reasons import StopReason
from agent_runtime.cancellation import CancelledError, run_with_cancellation

# StepGuard stall 检测：仅"可能修改文件"的工具才计入停滞
_MODIFYING_TOOLS = frozenset({"write_file", "patch_file", "run_shell"})


def _canonical_hash_for_trace(tool_name: str, args: dict) -> str:
    """死循环 trace 用的 args hash。"""
    import hashlib
    import json

    payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{tool_name}:{payload}".encode()).hexdigest()[:12]


def _log_loop(msg: str) -> None:
    """Loop 阶段 debug 日志（受 --log-level 控制）。"""
    from agent_runtime.logging_setup import get_logger

    get_logger("agent_loop").debug(msg.rstrip("\n"))


def _build_anthropic_tools(tools_registry: dict) -> list[dict]:
    """将内部工具注册表转换为 Anthropic tool_use 格式（仅 schema 字段）。"""
    from agent_runtime.tool_schema import tool_schema_view

    type_map = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
    result = []
    for name, spec in tool_schema_view(tools_registry).items():
        schema = spec.get("schema", {})
        properties = {}
        required = []
        for param, type_str in schema.items():
            if "=" in type_str:
                ptype, _, default = type_str.partition("=")
            else:
                ptype, default = type_str, None
            json_type = type_map.get(ptype, "string")
            prop = {"type": json_type}
            if default is not None:
                prop["default"] = default
            properties[param] = prop
            if default is None:
                required.append(param)
        result.append(
            {
                "name": name,
                "description": spec.get("description", ""),
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        )
    return result


class AgentLoop:
    """Agent 控制循环。管理对话回合，统计步数，产出 trace 工件。"""

    def __init__(self, agent, max_steps: int | None = None):
        self.agent = agent
        self.max_steps = max_steps or agent.config.max_steps
        self.stop_reason = ""
        self._task_state = None
        self._store = None
        self._last_token_meta = {}
        self._retry_count = 0
        self._call_timings: list[ModelCallTiming] = []
        self._in_flight_tool = ""
        self._tier_counts: dict[str, int] = {}
        self._tier_tools: dict[str, dict[str, int]] = {"host": {}, "container": {}}
        self._context_section_totals: dict[str, int] = {}
        self._context_built_count = 0
        self._context_cut_count = 0
        self._last_cache_key = ""
        self._cache_key_changes = 0
        self._plan_todos: list[dict] = []
        self._no_progress_steps = 0
        self._step_guard = StepGuard()
        self._json_retry_count = 0

    def _accumulate_context_stats(self, meta: dict) -> None:
        """从 context_built metadata 累积 section 统计 + cache hit rate。"""
        self._context_built_count += 1
        sections = meta.get("sections") or meta.get("context_sections") or {}
        for name, tokens in sections.items():
            try:
                self._context_section_totals[name] = (
                    self._context_section_totals.get(name, 0) + int(tokens)
                )
            except (ValueError, TypeError):
                pass
        cuts = meta.get("cuts") or []
        self._context_cut_count += len(cuts)
        cache_key = str(meta.get("prompt_cache_key", "") or "")
        if self._last_cache_key and cache_key != self._last_cache_key:
            self._cache_key_changes += 1
        self._last_cache_key = cache_key
        # emit 压缩触发事件
        pipe = meta.get("compression_pipeline") or {}
        for ev in pipe.get("compression_events", []):
            self._emit("compression_triggered", ev)

    def _build_memory_health(self) -> dict:
        """构建 report.json 中的 memory_health 字段。"""
        try:
            from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES
            from agent_runtime.features.memory.durable import DurableMemoryStore

            session = self.agent.session or {}
            episodic = session.get("episodic_notes", [])
            store = DurableMemoryStore(self.agent._cwd)
            prefs = store.get_preferences()
            avg_conf = round(sum(p.confidence for p in prefs) / max(len(prefs), 1), 2)
            return {
                "episodic_notes": len(episodic),
                "episodic_cap": MAX_EPISODIC_NOTES,
                "durable_entries": sum(1 for _ in store.topics_dir.glob("*.md") if store.topics_dir.is_dir()),
                "avg_confidence": avg_conf,
            }
        except Exception:
            return {}

    def _build_context_summary(self) -> dict:
        """构建 report.json 中的 context_summary 字段。"""
        build_count = self._context_built_count
        cache_hit_rate = 0.0
        if build_count > 0:
            cache_hit_rate = round(
                1.0 - (self._cache_key_changes / build_count), 3
            )
        return {
            "sections": dict(self._context_section_totals),
            "build_count": build_count,
            "cut_count": self._context_cut_count,
            "cache_hit_rate": cache_hit_rate,
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

            create_checkpoint(self.agent, ts, ts.user_request, trigger="user_cancel")
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
        self._sync_stop_reason(ts)
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
        if isinstance(error, RateLimitExceededError):
            ts.stop_with_reason(StopReason.RATE_LIMITED, "stopped", detail=str(error))
            return self._complete_run(ts, f"<final>API 限流：{error}</final>")
        from agent_runtime.providers.circuit_breaker import CircuitBreakerOpenError

        if isinstance(error, CircuitBreakerOpenError):
            ts.stop_with_reason(StopReason.CIRCUIT_BREAKER, "stopped", detail=str(error))
            return self._complete_run(ts, f"<final>API 熔断：{error}</final>")
        return None

    def _circuit_trace_listener(self, event: str, payload: dict) -> None:
        self._emit(event, payload)

    # ---- ReAct / 计时 ----

    def _record_model_timings(self, ts, timings: list, *, default_attempt: int = 1) -> None:
        if not timings:
            return
        self._call_timings.extend(timings)
        ttft_total = emit_model_timing_events(
            lambda event, payload: self._emit(event, payload),
            timings,
            default_attempt=default_attempt,
        )
        ts.node_timings["ttft_ms_total"] = int(ts.node_timings.get("ttft_ms_total", 0) or 0) + ttft_total

    def _notify(self, method: str, callback, **kwargs: object) -> None:
        """统一回调入口：XML 与 Native 路径共用。

        若 callback 为 None 或未实现 method，静默跳过。
        """
        if callback is None:
            return
        fn = getattr(callback, method, None)
        if fn is not None:
            fn(**kwargs)

    def _notify_react_phase(
        self,
        phase,
        *,
        step: int,
        path: ReactPath,
        tool: str | None = None,
        callback=None,
    ) -> None:
        from agent_runtime.react_phases import build_react_phase_payload

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
        return int(getattr(self.agent.config, "step_timeout_s", 0) or 0)

    # ---- 工具执行（XML / Native 共用）----

    def _run_tool_step(
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
    ) -> str:
        """执行工具、写 trace/history，返回下一轮 user_message。"""
        if (msg := self._abort_if_cancelled(ts, phase="pre_tool", in_flight=tool_name)) is not None:
            raise CancelledError("user", answer=msg)
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
        ts.record_tool(tool_name)
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
        self._in_flight_tool = tool_name
        try:
            result = self.agent.execute_tool(tool_name, tool_args)
        finally:
            self._in_flight_tool = ""
        if (msg := self._abort_if_cancelled(ts, phase="post_tool", in_flight=tool_name)) is not None:
            raise CancelledError("user", answer=msg)
        result_text = result.content if hasattr(result, "content") else str(result)
        te_ms = int((_time.time() - t0) * 1000)
        self._notify(
            "on_post_tool",
            callback,
            step=step,
            tool_name=tool_name,
            result_preview=result_text[:200],
            elapsed_ms=te_ms,
            path=str(path),
        )
        result_text = truncate_tool_result_for_agent(self.agent, tool_name, result_text)
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
        self.agent.update_memory_after_tool(tool_name, tool_args, result_text)
        self.agent.record({"role": "tool", "content": result_text, "tool_name": tool_name})
        self._record_tool_outcome(tool_name, result, ts)
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
        # 死循环检测：Gate 5.5 rejection → 升级为 stop
        error_code = result.metadata.get("tool_error_code", "")
        if error_code == "loop_detected":
            self._emit("loop_detected", {
                "tool": tool_name,
                "args_hash": _canonical_hash_for_trace(tool_name, tool_args),
                "window_size": int(
                    getattr(self.agent.config, "loop_detect_threshold", 3) or 3
                ),
            })
            ts.stop_with_reason(
                StopReason.CIRCUIT_BREAKER, "stopped",
                detail=f"死循环检测: {tool_name} 连续高频调用",
            )
            self.stop_reason = StopReason.CIRCUIT_BREAKER
            return ts.final_answer or f"任务因死循环检测终止（{tool_name}）。"

        # 每 tool 步 checkpoint（成功时），供 --resume 从最后成功步继续
        tool_success = result.metadata.get("tool_status") == "success"
        if tool_success:
            try:
                from agent_runtime.checkpoint import create_checkpoint

                create_checkpoint(self.agent, ts, ts.user_request, trigger="step_end")
            except Exception:
                pass
            self._advance_todo()

        # StepGuard 步进健康评估（stall + goal drift）— 每步都评估
        # 读类工具不产生 affected_paths 是正常的，不应计入 stall
        meta = result.metadata if hasattr(result, "metadata") else {}
        affected = meta.get("affected_paths", []) if isinstance(meta, dict) else []
        self._no_progress_steps = self._step_guard.stall_count
        guard_has_affected = bool(affected) or tool_name not in _MODIFYING_TOOLS
        verdict = self._step_guard.evaluate(
            StepContext(
                tool_name=tool_name,
                tool_args=tool_args,
                has_affected=guard_has_affected,
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
                ts.stop_with_reason(verdict.reason, "stopped", detail=verdict.detail)
                self.stop_reason = verdict.reason
                # 返回纯文本；调用方检测 self.stop_reason 作为终止信号
                return verdict.replan_hint or f"任务终止：{verdict.detail}"
            else:
                # warning 级（drift 预警，不终止）
                self._emit("goal_drift_warning", {"detail": verdict.detail})
        return f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"

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
        """Prompt 超出硬顶 8000 tokens 时返回错误消息。"""
        total = token_meta.get("total_tokens", 0) or token_meta.get("context_sections_total", 0)
        if total > 8000:
            return (
                f"<final>Prompt 大小 {total} tokens 超出硬顶限制 (8000)。"
                "请缩短输入或使用 /reset 清空对话历史后重试。</final>"
            )
        return None

    def _xml_build_context(self, ts, user_message: str, *, step: int, callback) -> str:
        t0 = _time.time()
        prompt_text, token_meta = self.agent._build_prompt_with_meta(user_message)
        if hard_limit := self._check_hard_cap(token_meta):
            return hard_limit
        from agent_runtime.message_projection import (
            attach_projection_metadata,
            build_context_prefix,
        )

        context_prefix = build_context_prefix(self.agent, token_meta)
        attach_projection_metadata(token_meta, self.agent.session, context_prefix=context_prefix)
        self._last_token_meta = token_meta
        self._accumulate_context_stats(token_meta)
        self._emit("context_built", build_trace_payload(token_meta))
        self._notify_react_phase(
            ReactPhase.REASONING,
            step=step,
            path="xml",
            callback=callback,
        )
        ts.node_timings.setdefault("prompt_build_ms", 0)
        ts.node_timings["prompt_build_ms"] += int((_time.time() - t0) * 1000)
        return prompt_text

    def _xml_call_model(self, ts, prompt_text: str, *, step: int) -> tuple[str, float]:
        ts.record_attempt()
        t1 = _time.time()
        self._emit(
            "model_request_start",
            {"step": ts.tool_steps + 1, "attempt": ts.attempts},
        )
        meta = getattr(self, "_last_token_meta", None) or {}
        cache_key = str(meta.get("prompt_cache_key", "") or "")
        raw = self._invoke_model_call(
            lambda: self.agent.circuit_breaker.call(
                self.agent.model_client.complete,
                prompt_text,
                max_new_tokens=self.agent.config.max_new_tokens,
                prompt_cache_key=cache_key,
            )
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
            f"  [loop] retry#{self._retry_count} backoff={delay}s "
            f"raw[:100]={raw.strip()[:100]}\n"
        )
        try:
            from pathlib import Path

            dbg = Path(self.agent._cwd) / ".agent" / "debug_retry.txt"
            dbg.parent.mkdir(parents=True, exist_ok=True)
            with open(dbg, "a", encoding="utf-8") as f:
                f.write(f"\n=== retry#{self._retry_count} ===\n{raw}\n")
        except Exception:
            pass
        for _ in range(int(delay * 10)):
            _time.sleep(0.1)
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
        retry = ParseRetry(build_recovery_prompt(failure), failure)
        return self._handle_parse_retry(ts, raw, retry, step=step)

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
            self._emit("plan_phase", {"source": "llm" if getattr(self.agent, "light_client", None) else "rule", "count": len(todos)})
            self._emit("plan_created", {"todos": list(todos)})
            self._start_next_todo()
        else:
            self._emit("plan_phase", {"source": "empty"})

    def _generate_plan(self, user_message: str) -> list[dict]:
        """用 light_client 或规则生成 TodoList。"""
        import json as _json

        # 尝试 light_client
        light = getattr(self.agent, "light_client", None)
        if light is not None:
            try:
                prompt = (
                    "将以下任务分解为 2-5 个步骤。只输出 JSON 数组：\n"
                    f"[{{\"id\":\"1\",\"content\":\"...\",\"status\":\"pending\"}},...]\n\n{user_message[:500]}"
                )
                raw = light.complete(prompt, max_new_tokens=256)
                start = raw.find("[")
                end = raw.rfind("]") + 1
                if start >= 0 and end > start:
                    todos = _json.loads(raw[start:end])
                    if isinstance(todos, list) and todos:
                        return todos
            except Exception:
                pass
        # 规则 fallback
        msg = user_message.lower()
        todos = []
        i = 1
        if any(w in msg for w in ("error", "fix", "bug", "repair", "修复")):
            todos.append({"id": str(i), "content": "Analyze error and locate suspect code", "status": "pending"})
            todos.append({"id": str(i + 1), "content": "Retrieve related context and tests", "status": "pending"})
            todos.append({"id": str(i + 2), "content": "Generate and apply fix patch", "status": "pending"})
            todos.append({"id": str(i + 3), "content": "Verify fix passes tests", "status": "pending"})
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

    # ---- 入口 ----

    def run(self, user_message: str, callback=None, *, skip_plan: bool = False) -> str:
        from agent_runtime.log_context import log_context
        from agent_runtime.task_state import TaskState

        shared = getattr(self.agent, "shared_run_id", None)
        agent_name = getattr(self.agent, "_agent_name", "") or "agent"
        ts = TaskState.create(user_request=user_message, run_id=shared)
        l2_agent = getattr(self.agent, "_l2_agent", "") or ""
        if shared and l2_agent:
            ts.task_id = getattr(self.agent, "_l2_task_id", "") or f"{shared}-{agent_name}"
            ts.l2_repair_run_id = getattr(self.agent, "_l2_repair_run_id", "") or shared
            ts.l2_agent = l2_agent
            ts.l2_phase = getattr(self.agent, "_l2_phase", "") or ""
            ts.l2_attempt = int(getattr(self.agent, "_l2_attempt", 0) or 0)
        elif shared:
            ts.task_id = f"{shared}-{agent_name}"
        self._task_state = ts
        self._call_timings = []
        self._retry_count = 0

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
                )

                if hasattr(self.agent.model_client, "chat_with_tools"):
                    answer = self._run_with_native_tools(user_message, ts, callback)
                else:
                    answer = self._run_with_text_parsing(user_message, ts, callback)
                # Memory Dream: ask() 结束后自动维护记忆
                self._run_memory_dream()
                return answer
        finally:
            cb.remove_listener(self._circuit_trace_listener)

    def _run_with_native_tools(self, user_message: str, ts, callback=None) -> str:
        step_timeout_s = self._step_timeout_limit_s()
        step_clock = StepClock(step_timeout_s)
        if (msg := self._maybe_step_timeout(ts, step_clock, 1, "native")) is not None:
            return msg

        system_prompt, user_message, budget_meta = self.agent.build_for_native(user_message)
        if hard_limit := self._check_hard_cap(budget_meta):
            return hard_limit
        from agent_runtime.message_projection import (
            attach_projection_metadata,
            build_context_prefix,
        )

        context_prefix = build_context_prefix(self.agent, budget_meta)
        attach_projection_metadata(
            budget_meta, self.agent.session, context_prefix=context_prefix
        )
        self._last_token_meta = budget_meta
        self._last_budget_meta = budget_meta
        self._accumulate_context_stats(budget_meta)
        self._emit("context_built", build_trace_payload(budget_meta))
        tools_def = _build_anthropic_tools(self.agent.tools)

        if not system_prompt:
            from agent_runtime.prompt_prefix import cache_stable_text

            prefix = self.agent._prefix
            system_prompt = cache_stable_text(
                getattr(prefix, "stable_system_text", "") or "",
                getattr(prefix, "stable_tools_text", "") or "",
            ) or getattr(prefix, "stable_text", "")

        def phase_hook(phase, *, step: int, tool: str | None = None) -> None:
            nonlocal step_clock
            if phase == ReactPhase.REASONING:
                if (msg := self._abort_if_cancelled(ts, phase="native_reasoning")) is not None:
                    raise CancelledError("user", answer=msg)
                if step > 1:
                    step_clock = StepClock(step_timeout_s)
                step_clock.check(step=step, path="native")
                if callback is not None:
                    self._notify("on_step_start", callback, step=step, max_steps=self.max_steps, path="native")
            elif phase == ReactPhase.ACTING:
                step_clock.check(step=step, path="native")
            self._notify_react_phase(
                phase,
                step=step,
                path="native",
                tool=tool,
                callback=callback,
            )

        def executor(tool_name: str, tool_input: dict) -> str:
            step = ts.tool_steps + 1
            return self._run_tool_step(
                ts,
                tool_name,
                tool_input,
                step=step,
                path="native",
                callback=callback,
                record_assistant=False,
                emit_acting=False,
                emit_observation=False,
                emit_recording=True,
            )

        def after_model(step: int) -> None:
            step_clock.check(step=step, path="native")

        t0 = _time.time()
        self._emit("model_request_start", {"step": 1, "attempt": 1})
        self._notify(
            "on_pre_model", callback, step=1,
            prompt_preview=user_message[:200], path="native",
        )
        try:
            result = self._invoke_model_call(
                lambda: self.agent.model_client.chat_with_tools(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    tools=tools_def,
                    executor=executor,
                    max_turns=self.max_steps,
                    phase_hook=phase_hook,
                    step_boundary_hook=after_model,
                    cancel_token=self._cancel_token,
                )
            )
            if isinstance(result, tuple):
                answer, call_usage = result
            else:
                answer = result
                call_usage = getattr(self.agent.model_client, "last_call_usage", {}) or {}
            self._apply_call_usage_meta(call_usage)
            self._record_model_timings(
                ts, collect_client_timings(self.agent.model_client), default_attempt=1
            )
        except StepTimeoutError as e:
            return self._finish_step_timeout(ts, e, clock=step_clock)
        except CancelledError as e:
            if e.answer:
                return e.answer
            return self._finish_user_cancel(ts, phase="model_wait")
        except Exception as e:
            if (msg := self._stop_for_api_error(ts, e)) is not None:
                return msg
            ts.stop_with_reason(StopReason.API_ERROR, "failed", detail=f"error: {e}")
            return self._complete_run(ts, f"<final>API 错误: {e}</final>")

        elapsed_ms = int((_time.time() - t0) * 1000)
        ts.node_timings["model_call_ms"] = elapsed_ms
        self._notify(
            "on_post_model", callback, step=1,
            raw_preview=answer[:200], elapsed_ms=elapsed_ms, path="native",
        )

        if answer.strip() == NATIVE_MAX_TURNS_MESSAGE:
            ts.stop_step_limit(self.max_steps)
            return self._complete_run(
                ts,
                f"<final>已达到最大工具调用步数限制({self.max_steps})，当前任务未完成。</final>",
            )

        self.agent.record({"role": "assistant", "content": answer})
        # final_answer schema 校验（Native 路径：仅校验 + trace，不重试）
        ok, err_msg = self._validate_final_answer(answer)
        if not ok:
            self._emit("json_validation_warning", {"error": err_msg})
        # 若 StepGuard 已设置 stop_reason（stall/goal_drift），保留之
        if not self.stop_reason:
            ts.finish_success(answer)
        _log_loop(f"  [loop] final ({elapsed_ms}ms total)\n")
        return self._complete_run(
            ts,
            answer,
            recording={
                "step": max(ts.tool_steps, 1),
                "path": "native",
                "callback": callback,
            },
        )

    def _run_with_text_parsing(self, user_message: str, ts, callback=None) -> str:
        while True:
            if (msg := self._abort_if_cancelled(ts, phase="step_start")) is not None:
                return msg
            if (msg := self._check_xml_loop_limits(ts)) is not None:
                return msg

            step = ts.tool_steps + 1
            step_clock = StepClock(self._step_timeout_limit_s())
            if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                return msg
            if callback is not None:
                self._notify("on_step_start", callback, step=step, max_steps=self.max_steps, path="xml")

            prompt_text = self._xml_build_context(ts, user_message, step=step, callback=callback)

            self._notify(
                "on_pre_model", callback, step=step,
                prompt_preview=prompt_text[:200], path="xml",
            )
            try:
                raw, t1 = self._xml_call_model(ts, prompt_text, step=step)
            except CancelledError:
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
                "on_post_model", callback, step=step,
                raw_preview=raw[:200], elapsed_ms=t_parse, path="xml",
            )

            if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                return msg

            kind, payload = self.agent.parse(raw)

            if kind == "final":
                _log_loop(f"  [loop] final ({t_parse}ms parse)\n")
                final_text = str(payload)
                ok, err_msg = self._validate_final_answer(final_text)
                if not ok and self._json_retry_count < self.MAX_JSON_RETRIES:
                    self._json_retry_count += 1
                    self._emit("json_retry", {
                        "attempt": self._json_retry_count,
                        "error": err_msg,
                    })
                    user_message = err_msg
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
                    user_message = self._xml_invalid_tool_retry(
                        ts, payload, raw=raw, step=step
                    )
                    continue
                if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                    return msg
                tool_name = payload.get("name", "unknown")
                tool_args = payload.get("args", {})
                try:
                    user_message = self._run_tool_step(
                        ts,
                        tool_name,
                        tool_args,
                        step=step,
                        path="xml",
                        callback=callback,
                    )
                except CancelledError as e:
                    return e.answer
                if self.stop_reason:
                    # StepGuard 触发终止（stall / goal_drift）
                    return self._complete_run(
                        ts, user_message,
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
        """从 plan_todos + task_summary + traceback 中提取 suspect 文件名。"""
        import re

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

        # 3. 从 traceback（在 user request 中）提取
        user_req = self._task_state.user_request if self._task_state else ""
        tb_match = re.search(r'File\s+"([^"]+)"', user_req)
        if tb_match:
            files.add(tb_match.group(1).split("/")[-1].split("\\")[-1])

        return files

    MAX_JSON_RETRIES = 2

    def _validate_final_answer(self, text: str) -> tuple[bool, str]:
        """校验 final answer 的 JSON 语法与可选 schema。

        Returns:
            (ok, error_message)。ok=True 表示通过，error_message 为 recovery 提示。
        """
        import json as _json

        config = self.agent.config
        if not config.json_mode:
            return True, ""
        schema = config.final_schema

        # L1: JSON 语法
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError as e:
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
            type_map = {"str": str, "int": int, "float": (int, float), "bool": bool,
                        "list": list, "dict": dict}
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
            prefix = cleaned[:m.start()].strip()
            if prefix:
                cleaned = cleaned[m.start():]

        cleaned = cleaned.strip()
        return cleaned if cleaned else raw.strip()

    def _run_memory_dream(self) -> None:
        """Agent ask() 结束后执行 Memory Dream：去重 + 过期 + 裁剪。"""
        mem = self.agent.session.get("memory")
        if not mem:
            return
        from agent_runtime.features.memory.dream import dream_summary_to_trace, run_memory_dream

        root = getattr(self.agent, "_cwd", "") or ""
        stats = run_memory_dream(mem, durable_root=root)
        if any(stats.get(k, 0) for k in ("deduped", "expired", "trimmed")):
            self._emit("memory_dream", dream_summary_to_trace(stats))

    def _get_store(self):
        if self._store is None:
            from agent_runtime.run_store import RunStore

            self._store = RunStore(root=self.agent._cwd)
        return self._store

    def _record_tool_outcome(self, tool_name: str, result, ts) -> None:
        meta = getattr(result, "metadata", None) or {}
        ts.record_tool_rejection(tool_name, meta)
        self._emit_tool_trace(tool_name, result)

    def _emit_tool_trace(self, tool_name: str, result) -> None:
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
        # Localizer 工具顺序检测：ast_parse 前应先调 stack_parse
        agent_name = getattr(self.agent, "_agent_name", "") or ""
        if agent_name == "localizer" and tool_name == "ast_parse":
            history = self.agent.session.get("history", [])
            called_tools = {h.get("tool_name") for h in history if h.get("tool_name")}
            if "stack_parse" not in called_tools:
                self._emit("tool_order_warning", {
                    "agent": "localizer",
                    "tool": "ast_parse",
                    "expected_before": "stack_parse",
                })
        preview = meta.get("patch_preview")
        if preview:
            self._emit("tool_preview", {"tool": tool_name, **preview})
        self._emit("tool_executed", tool_trace_payload(tool_name, meta))

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
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.features.memory import promote_durable_memory
        from agent_runtime.session_store import SessionStore

        try:
            store = self._get_store()
            shared = getattr(self.agent, "shared_run_id", None)
            agent_name = getattr(self.agent, "_agent_name", "") or "agent"
            session_usage = getattr(self.agent.model_client, "session_usage", None) or {}
            from agent_runtime.token_accounting import build_report_token_fields

            report_token = build_report_token_fields(session_usage, self._last_token_meta)
            report_latency = build_report_latency_fields(self._call_timings)
            self.agent._last_run_node_timings = dict(ts.node_timings)
            self.agent._last_call_timings = list(self._call_timings)
            if report_token.get("prompt_budget") is None:
                report_token["prompt_budget"] = getattr(self.agent.config, "prompt_budget", 0)
            report_body = {
                "run_id": ts.run_id,
                "agent": agent_name,
                "tool_steps": ts.tool_steps,
                "attempts": ts.attempts,
                "stop_reason": ts.stop_reason,
                "status": ts.status,
                "prompt_cache_key": getattr(self.agent._prefix, "hash", ""),
                "node_timings": ts.node_timings,
                "tier_summary": {
                    "host_calls": self._tier_counts.get("host", 0),
                    "container_calls": self._tier_counts.get("container", 0),
                    "host_tools": self._tier_tools.get("host", {}),
                    "container_tools": self._tier_tools.get("container", {}),
                },
                "context_summary": self._build_context_summary(),
                "retry_summary": {
                    "parse_retries": self._retry_count,
                    "model_attempts": ts.attempts,
                    "tool_steps": ts.tool_steps,
                },
                "quota_usage": (
                    self.agent.quota.quota_summary()
                    if getattr(self.agent, "quota", None)
                    else {}
                ),
                "plan_todos": list(self._plan_todos),
                "memory_health": self._build_memory_health(),
                **report_token,
                **report_latency,
                **ts.rejection_report_fields(),
            }
            if ts.l2_agent:
                report_body.update(
                    {
                        "task_id": ts.task_id,
                        "l2_repair_run_id": ts.l2_repair_run_id,
                        "l2_agent": ts.l2_agent,
                        "l2_phase": ts.l2_phase,
                        "l2_attempt": ts.l2_attempt,
                    }
                )
            self.agent._last_task_state = ts
            if shared:
                store.write_task_state_named(shared, f"task_state.{agent_name}.json", ts)
                store.write_agent_report(shared, agent_name, report_body)
            else:
                trigger = (
                    "user_cancel"
                    if ts.stop_reason == StopReason.USER_CANCEL.value
                    else "ask_end"
                )
                cp = create_checkpoint(self.agent, ts, ts.user_request, trigger=trigger)
                ts.checkpoint_id = cp.get("run_id", "") if cp else ""
                store.write_task_state(ts)
                compress_stats = store.compress_trace_if_needed(ts.run_id)
                if compress_stats:
                    report_body["trace_compressed"] = True
                    report_body["trace_compression"] = compress_stats
                store.write_report(ts, report_body)
            promote_durable_memory(
                ts.user_request,
                ts.final_answer,
                root=self.agent._cwd,
            )
            SessionStore(root=self.agent._cwd).save(self.agent.session)
        except Exception:
            pass
