"""Agent 控制循环：感知 → 决策 → 行动 → 记录 → 循环。

停机后产出 task_state.json + trace.jsonl + report.json（含 node_timings 耗时分布）。
"""

import time as _time


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

    def run(self, user_message: str, callback=None) -> str:
        from agent_runtime.task_state import TaskState

        ts = TaskState.create(user_request=user_message)
        self._task_state = ts
        self._emit("run_started")

        self.agent.record({"role": "user", "content": user_message})

        # 用轻量模型生成一句话任务摘要（如可用）
        self._gen_task_summary(user_message)

        while True:
            # 停机检查
            if ts.tool_steps > self.max_steps:
                self.stop_reason = f"tool_steps >= {self.max_steps}"
                ts.stop_step_limit(self.max_steps)
                self._emit("run_finished", {"stop_reason": self.stop_reason})
                self._finalize_run(ts)
                return (
                    "<final>已达到最大工具调用步数限制"
                    f"({self.max_steps})，当前任务未完成。</final>"
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

            # 2. 调用模型（经 CircuitBreaker 包裹，附 cache key）
            ts.record_attempt()
            t1 = _time.time()
            cache_key = getattr(self.agent._prefix, "hash", "")
            try:
                raw = self.agent.circuit_breaker.call(
                    self.agent.model_client.complete,
                    prompt_text,
                    max_new_tokens=self.agent.config.max_new_tokens,
                    prompt_cache_key=cache_key,
                )
                ts.node_timings.setdefault("model_call_ms", 0)
                ts.node_timings["model_call_ms"] += int((_time.time() - t1) * 1000)
            except Exception as e:
                if "Circuit breaker is open" in str(e):
                    self.stop_reason = "circuit_breaker"
                    return f"<final>API 熔断：{e}</final>"
                raise

            # 3. 解析输出
            kind, payload = self.agent.parse(raw)

            # 4. 根据解析结果决定下一步
            if kind == "final":
                self.agent.record({"role": "assistant", "content": str(payload)})
                self.stop_reason = "final"
                ts.finish_success(str(payload))
                self._emit("run_finished", {"stop_reason": "final"})
                self._finalize_run(ts)
                return str(payload)

            elif kind == "tool":
                tool_name = payload.get("name", "unknown")
                tool_args = payload.get("args", {})
                ts.record_tool(tool_name)

                self.agent.record({
                    "role": "assistant",
                    "content": f"调用工具: {tool_name}",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                })

                t2 = _time.time()
                result = self.agent.execute_tool(tool_name, tool_args)
                result_text = (
                    result.content if hasattr(result, 'content') else str(result)
                )
                ts.node_timings.setdefault("tool_exec_ms", 0)
                ts.node_timings["tool_exec_ms"] += int((_time.time() - t2) * 1000)
                self.agent.update_memory_after_tool(tool_name, tool_args, result_text)
                self.agent.record({"role": "tool", "content": result_text})
                self._emit("tool_executed", {"tool": tool_name})

                if callback:
                    callback.on_tool_executed(tool_name, result_text)

                user_message = (
                    f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"
                )

            elif kind == "retry":
                # 指数退避：1s→2s→4s→8s（防密集重试浪费配额）
                self._retry_count += 1
                _time.sleep(min(2 ** (self._retry_count - 1), 8))
                self.agent.record({"role": "system", "content": str(payload)})
                user_message = str(payload)

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

    def _emit(self, event: str, payload: dict | None = None):
        """发送 trace 事件到 RunStore。"""
        try:
            if self._task_state:
                self._get_store().append_trace(self._task_state, event, payload)
        except Exception:
            pass

    def _finalize_run(self, ts):
        """完成 run：写入工件 + checkpoint + durable memory + session 保存。"""
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.features.memory import promote_durable_memory
        from agent_runtime.session_store import SessionStore

        try:
            store = self._get_store()
            cp = create_checkpoint(self.agent, ts, ts.user_request, trigger="ask_end")
            ts.checkpoint_id = cp.get("run_id", "") if cp else ""
            store.write_task_state(ts)
            store.write_report(ts, {
                "run_id": ts.run_id,
                "tool_steps": ts.tool_steps,
                "attempts": ts.attempts,
                "stop_reason": ts.stop_reason,
                "status": ts.status,
                "prompt_cache_key": getattr(self.agent._prefix, "hash", ""),
                "node_timings": ts.node_timings,
                "token_usage": self._last_token_meta.get("sections", {}),
                "total_tokens": self._last_token_meta.get("total_tokens", 0),
            })
            promote_durable_memory(
                ts.user_request, ts.final_answer, root=self.agent._cwd,
            )
            # 自动保存会话（支持 --resume）
            SessionStore(root=self.agent._cwd).save(self.agent.session)
        except Exception:
            pass
