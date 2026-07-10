"""Agent 控制循环：感知 → 决策 → 行动 → 记录 → 循环。

停机后产出 task_state.json + trace.jsonl + report.json（含 node_timings 耗时分布）。
"""

import sys as _sys
import time as _time

from agent_runtime.compression_pipeline import truncate_tool_result_for_agent


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
            # 解析 "type=default" 格式
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
        self._retry_count = 0  # 最近一次 prompt 的 token 元数据
        self._call_timings: list = []

    def _collect_client_timings(self) -> list:
        """Read per-call timings recorded by the model client."""
        client = self.agent.model_client
        timings = getattr(client, "last_call_timings", None) or []
        if timings:
            return list(timings)
        single = getattr(client, "last_call_timing", None)
        return [single] if single else []

    def _record_model_timings(self, ts, timings: list, *, default_attempt: int = 1) -> None:
        """Merge timings into loop state and emit trace events."""
        if not timings:
            return
        self._call_timings.extend(timings)
        ttft_total = 0
        for index, timing in enumerate(timings):
            if hasattr(timing, "to_dict"):
                fields = timing.to_dict()
            else:
                fields = dict(timing)
            step = int(fields.get("step", 0) or index + 1)
            attempt = int(fields.get("attempt", 0) or default_attempt)
            ttft_ms = int(fields.get("ttft_ms", 0) or 0)
            total_ms = int(fields.get("total_ms", 0) or 0)
            output_tokens = int(fields.get("output_tokens", 0) or 0)
            ttft_total += ttft_ms
            self._emit(
                "model_first_token",
                {"ttft_ms": ttft_ms, "step": step, "attempt": attempt},
            )
            self._emit(
                "model_complete",
                {
                    "total_ms": total_ms,
                    "output_tokens": output_tokens,
                    "step": step,
                    "attempt": attempt,
                },
            )
        ts.node_timings["ttft_ms_total"] = int(ts.node_timings.get("ttft_ms_total", 0) or 0) + ttft_total

    def run(self, user_message: str, callback=None) -> str:
        """执行一次 Agent 任务：ReAct 循环直至 final answer 或 max_steps。

        Args:
            user_message: 用户输入。
            callback: 可选 ProgressCallback，用于 CLI 进度输出。

        Returns:
            模型最终回答文本。
        """
        from agent_runtime.log_context import log_context
        from agent_runtime.task_state import TaskState

        shared = getattr(self.agent, "shared_run_id", None)
        ts = TaskState.create(user_request=user_message, run_id=shared)
        agent_name = getattr(self.agent, "_agent_name", "") or "agent"
        if shared:
            ts.task_id = f"{shared}-{agent_name}"
        self._task_state = ts
        self._call_timings = []

        with log_context(run_id=ts.run_id, agent=agent_name):
            self._emit("run_started")

            self.agent.record({"role": "user", "content": user_message})
            self._gen_task_summary(user_message)

            # 如果模型客户端支持原生 tool_use，使用 API 原生协议（免文本解析）
            if hasattr(self.agent.model_client, "chat_with_tools"):
                return self._run_with_native_tools(user_message, ts, callback)

            # 降级：传统文本解析模式
            return self._run_with_text_parsing(user_message, ts, callback)

    def _run_with_native_tools(self, user_message: str, ts, callback=None) -> str:
        """使用 API 原生 tool_use 协议（Anthropic 兼容）。"""
        system_prompt, user_message, budget_meta = self.agent.build_for_native(user_message)
        self._last_budget_meta = budget_meta
        # 构建 Anthropic 格式的工具定义
        tools_def = _build_anthropic_tools(self.agent.tools)

        # 系统提示词（仅 stable 段，便于 prompt cache）
        system_prompt = system_prompt or getattr(self.agent._prefix, "stable_text", "")

        # 工具执行回调
        def executor(tool_name: str, tool_input: dict) -> str:
            ts.record_tool(tool_name)
            ts.node_timings.setdefault("tool_exec_ms", 0)
            t0 = _time.time()
            result = self.agent.execute_tool(tool_name, tool_input)
            result_text = result.content if hasattr(result, "content") else str(result)
            result_text = truncate_tool_result_for_agent(self.agent, tool_name, result_text)
            te_ms = int((_time.time() - t0) * 1000)
            ts.node_timings["tool_exec_ms"] += te_ms
            _log_loop(f"  [loop] {tool_name} tool={te_ms}ms\n")
            self.agent.update_memory_after_tool(tool_name, tool_input, result_text)
            self._record_tool_outcome(tool_name, result, ts)
            if callback:
                callback.on_tool_executed(tool_name, result_text)
            return result_text

        t0 = _time.time()
        self._emit("model_request_start", {"step": 1, "attempt": 1})
        try:
            result = self.agent.model_client.chat_with_tools(
                system_prompt=system_prompt,
                user_message=user_message,
                tools=tools_def,
                executor=executor,
                max_turns=self.max_steps,
            )
            if isinstance(result, tuple):
                answer, call_usage = result
            else:
                answer = result
                call_usage = getattr(self.agent.model_client, "last_call_usage", {}) or {}
            self._apply_call_usage_meta(call_usage)
            self._record_model_timings(ts, self._collect_client_timings(), default_attempt=1)
        except Exception as e:
            self.stop_reason = f"error: {e}"
            return f"<final>API 错误: {e}</final>"

        elapsed_ms = int((_time.time() - t0) * 1000)
        ts.node_timings["model_call_ms"] = elapsed_ms

        self.agent.record({"role": "assistant", "content": answer})
        self.stop_reason = "final"
        ts.finish_success(answer)
        self._emit("run_finished", {"stop_reason": "final"})
        self._finalize_run(ts)

        _log_loop(f"  [loop] final ({elapsed_ms}ms total)\n")
        return answer

    def _run_with_text_parsing(self, user_message: str, ts, callback=None) -> str:
        """传统文本解析模式（降级路径）。"""
        while True:
            if ts.tool_steps > self.max_steps:
                self.stop_reason = f"tool_steps >= {self.max_steps}"
                ts.stop_step_limit(self.max_steps)
                self._emit("run_finished", {"stop_reason": self.stop_reason})
                self._finalize_run(ts)
                return (
                    f"<final>已达到最大工具调用步数限制({self.max_steps})，当前任务未完成。</final>"
                )

            if ts.attempts >= self.max_steps * 3 + 4:
                self.stop_reason = f"attempts >= {self.max_steps * 3 + 4}"
                ts.stop_retry_limit(self.max_steps * 3 + 4)
                self._emit("run_finished", {"stop_reason": self.stop_reason})
                self._finalize_run(ts)
                return (
                    "<final>模型输出格式错误次数过多，已终止。"
                    "请检查 System Prompt 中的工具调用格式说明。</final>"
                )

            # 1. 组装 prompt
            t0 = _time.time()
            prompt_text, token_meta = self.agent._build_prompt_with_meta(user_message)
            self._last_token_meta = token_meta
            ts.node_timings.setdefault("prompt_build_ms", 0)
            ts.node_timings["prompt_build_ms"] += int((_time.time() - t0) * 1000)

            # 2. 调用模型
            ts.record_attempt()
            t1 = _time.time()
            self._emit(
                "model_request_start",
                {"step": ts.tool_steps + 1, "attempt": ts.attempts},
            )
            try:
                raw = self.agent.circuit_breaker.call(
                    self.agent.model_client.complete,
                    prompt_text,
                    max_new_tokens=self.agent.config.max_new_tokens,
                )
                ts.node_timings.setdefault("model_call_ms", 0)
                ts.node_timings["model_call_ms"] += int((_time.time() - t1) * 1000)
                self._record_model_timings(
                    ts,
                    self._collect_client_timings(),
                    default_attempt=ts.attempts,
                )
            except Exception as e:
                if "Circuit breaker is open" in str(e):
                    self.stop_reason = "circuit_breaker"
                    return f"<final>API 熔断：{e}</final>"
                raise

            # 3. 解析输出
            kind, payload = self.agent.parse(raw)
            t_parse = int((_time.time() - t1) * 1000)

            if kind == "final":
                _log_loop(f"  [loop] final ({t_parse}ms parse)\n")
                self.agent.record({"role": "assistant", "content": str(payload)})
                self.stop_reason = "final"
                ts.finish_success(str(payload))
                self._emit("run_finished", {"stop_reason": "final"})
                self._finalize_run(ts)
                return str(payload)

            elif kind == "tool":
                if not isinstance(payload, dict) or "name" not in payload:
                    self.agent.record({"role": "system", "content": "工具调用格式错误"})
                    user_message = "工具调用格式错误，请重试。"
                    continue
                tool_name = payload.get("name", "unknown")
                tool_args = payload.get("args", {})
                ts.record_tool(tool_name)
                self.agent.record(
                    {
                        "role": "assistant",
                        "content": f"调用工具: {tool_name}",
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    }
                )
                t2 = _time.time()
                result = self.agent.execute_tool(tool_name, tool_args)
                result_text = result.content if hasattr(result, "content") else str(result)
                result_text = truncate_tool_result_for_agent(self.agent, tool_name, result_text)
                te_ms = int((_time.time() - t2) * 1000)
                ts.node_timings.setdefault("tool_exec_ms", 0)
                ts.node_timings["tool_exec_ms"] += te_ms
                _log_loop(f"  [loop] {tool_name} tool={te_ms}ms\n")
                self.agent.update_memory_after_tool(tool_name, tool_args, result_text)
                self.agent.record(
                    {"role": "tool", "content": result_text, "tool_name": tool_name}
                )
                self._record_tool_outcome(tool_name, result, ts)
                if callback:
                    callback.on_tool_executed(tool_name, result_text)
                user_message = f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"

            elif kind == "retry":
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
                self.agent.record({"role": "system", "content": str(payload)})
                user_message = str(payload)

    def _merge_budget_meta(self, meta: dict) -> None:
        """将 TokenBudget 裁剪信息并入 token 元数据。"""
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
        """将 chat_with_tools 返回的 API usage 写入 _last_token_meta。"""
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
        """用轻量模型生成一句话任务摘要。"""
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
        """获取缓存的 RunStore 实例。"""
        if self._store is None:
            from agent_runtime.run_store import RunStore

            self._store = RunStore(root=self.agent._cwd)
        return self._store

    def _record_tool_outcome(self, tool_name: str, result, ts) -> None:
        """记录工具拒绝统计并写入 trace。"""
        meta = getattr(result, "metadata", None) or {}
        ts.record_tool_rejection(tool_name, meta)
        self._emit_tool_trace(tool_name, result)

    def _emit_tool_trace(self, tool_name: str, result) -> None:
        """写入 tool_preview（若有）与 tool_executed trace 事件。"""
        from agent_runtime.tool_rejection import tool_trace_payload

        meta = getattr(result, "metadata", None) or {}
        preview = meta.get("patch_preview")
        if preview:
            self._emit("tool_preview", {"tool": tool_name, **preview})
        self._emit("tool_executed", tool_trace_payload(tool_name, meta))

    def _emit(self, event: str, payload: dict | None = None):
        """发送 trace 事件到 RunStore。"""
        try:
            payload = dict(payload or {})
            agent_name = getattr(self.agent, "_agent_name", "") or "agent"
            payload.setdefault("agent", agent_name)
            shared = getattr(self.agent, "shared_run_id", None)
            ts = self._task_state
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
        """完成 run：写入工件 + checkpoint + durable memory + session 保存。"""
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.features.memory import promote_durable_memory
        from agent_runtime.session_store import SessionStore

        try:
            store = self._get_store()
            shared = getattr(self.agent, "shared_run_id", None)
            agent_name = getattr(self.agent, "_agent_name", "") or "agent"
            session_usage = getattr(self.agent.model_client, "session_usage", None) or {}
            from agent_runtime.model_timing import build_report_latency_fields
            from agent_runtime.token_accounting import build_report_token_fields

            report_token = build_report_token_fields(session_usage, self._last_token_meta)
            report_latency = build_report_latency_fields(self._call_timings)
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
                **report_token,
                **report_latency,
                **ts.rejection_report_fields(),
            }
            if shared:
                store.write_task_state_named(shared, f"task_state.{agent_name}.json", ts)
                store.write_agent_report(shared, agent_name, report_body)
            else:
                cp = create_checkpoint(self.agent, ts, ts.user_request, trigger="ask_end")
                ts.checkpoint_id = cp.get("run_id", "") if cp else ""
                store.write_task_state(ts)
                store.write_report(ts, report_body)
            promote_durable_memory(
                ts.user_request,
                ts.final_answer,
                root=self.agent._cwd,
            )
            SessionStore(root=self.agent._cwd).save(self.agent.session)
        except Exception:
            pass
