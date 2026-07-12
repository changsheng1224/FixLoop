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
from agent_runtime.stop_reasons import StopReason
from agent_runtime.cancellation import CancelledError, run_with_cancellation


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
        if callback is None:
            return
        on_phase = getattr(callback, "on_react_phase", None)
        if on_phase is not None:
            on_phase(str(phase), step, self.max_steps, tool=tool or "")

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
        result_text = truncate_tool_result_for_agent(self.agent, tool_name, result_text)
        te_ms = int((_time.time() - t0) * 1000)
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
        if callback:
            callback.on_tool_executed(tool_name, result_text)
        # 每 tool 步 checkpoint（成功时），供 --resume 从最后成功步继续
        if result.metadata.get("tool_status") == "success":
            try:
                from agent_runtime.checkpoint import create_checkpoint

                create_checkpoint(self.agent, ts, ts.user_request, trigger="step_end")
            except Exception:
                pass
            self._advance_todo()
            self._no_progress_steps = 0
        else:
            # 非成功或只读无副作用工具 → 累计无进展步数
            meta = result.metadata if hasattr(result, "metadata") else {}
            affected = meta.get("affected_paths", []) if isinstance(meta, dict) else []
            if not affected:
                self._no_progress_steps += 1
                if self._no_progress_steps >= 3:
                    self._emit("stall_detected", {
                        "steps": self._no_progress_steps,
                        "last_tool": tool_name,
                    })
                    # mark 当前 in_progress todo 为 blocked
                    for todo in self._plan_todos:
                        if todo.get("status") == "in_progress":
                            todo["status"] = "blocked"
                            self._emit("todo_updated", {"todo": dict(todo)})
                            break
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

    def _xml_build_context(self, ts, user_message: str, *, step: int, callback) -> str:
        t0 = _time.time()
        prompt_text, token_meta = self.agent._build_prompt_with_meta(user_message)
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

    def _generate_plan(self, user_message: str) -> list[dict]:
        """用 light_client 或规则生成 TodoList。"""
        # 尝试 light_client
        light = getattr(self.agent, "light_client", None)
        if light is not None:
            try:
                prompt = (
                    "将以下任务分解为 2-5 个步骤。只输出 JSON 数组：\n"
                    f"[{{\"id\":\"1\",\"content\":\"...\",\"status\":\"pending\"}},...]\n\n{user_message[:500]}"
                )
                raw = light.complete(prompt, max_new_tokens=256)
                # 提取 JSON 数组
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

    def run(self, user_message: str, callback=None) -> str:
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
                self._plan_todos = self._generate_plan(user_message)
                self._no_progress_steps = 0
                if self._plan_todos:
                    self.agent.session["plan_todos"] = self._plan_todos
                    self._emit("plan_created", {"todos": list(self._plan_todos)})
                    self._start_next_todo()

                if hasattr(self.agent.model_client, "chat_with_tools"):
                    return self._run_with_native_tools(user_message, ts, callback)
                return self._run_with_text_parsing(user_message, ts, callback)
        finally:
            cb.remove_listener(self._circuit_trace_listener)

    def _run_with_native_tools(self, user_message: str, ts, callback=None) -> str:
        step_timeout_s = self._step_timeout_limit_s()
        step_clock = StepClock(step_timeout_s)
        if (msg := self._maybe_step_timeout(ts, step_clock, 1, "native")) is not None:
            return msg

        system_prompt, user_message, budget_meta = self.agent.build_for_native(user_message)
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
                    callback.on_step_start(step, self.max_steps)
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

        if answer.strip() == NATIVE_MAX_TURNS_MESSAGE:
            ts.stop_step_limit(self.max_steps)
            return self._complete_run(
                ts,
                f"<final>已达到最大工具调用步数限制({self.max_steps})，当前任务未完成。</final>",
            )

        self.agent.record({"role": "assistant", "content": answer})
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
                callback.on_step_start(step, self.max_steps)

            prompt_text = self._xml_build_context(ts, user_message, step=step, callback=callback)

            try:
                raw, t1 = self._xml_call_model(ts, prompt_text, step=step)
            except CancelledError:
                return self._finish_user_cancel(ts, phase="model_wait")
            except Exception as e:
                if (msg := self._stop_for_api_error(ts, e)) is not None:
                    return msg
                raise

            if (msg := self._abort_if_cancelled(ts, phase="post_model")) is not None:
                return msg

            if (msg := self._maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
                return msg

            kind, payload = self.agent.parse(raw)
            t_parse = int((_time.time() - t1) * 1000)

            if kind == "final":
                _log_loop(f"  [loop] final ({t_parse}ms parse)\n")
                self.agent.record({"role": "assistant", "content": str(payload)})
                ts.finish_success(str(payload))
                return self._complete_run(
                    ts,
                    str(payload),
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
