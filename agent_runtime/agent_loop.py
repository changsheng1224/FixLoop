"""Agent 控制循环：感知 → 决策 → 行动 → 记录 → 循环。

每个循环周期：
1. 组装 prompt → 2. 调模型 → 3. 解析 → 4. tool/final/retry
停机后产出 task_state.json + trace.jsonl + report.json。
"""


class AgentLoop:
    """Agent 控制循环。管理对话回合，统计步数，产出 trace 工件。"""

    def __init__(self, agent, max_steps: int | None = None):
        self.agent = agent
        self.max_steps = max_steps or agent.config.max_steps
        self.tool_steps = 0
        self.attempts = 0
        self.stop_reason = ""
        self._task_state = None

    def run(self, user_message: str, callback=None) -> str:
        from agent_runtime.task_state import TaskState

        ts = TaskState.create(user_request=user_message)
        self._task_state = ts
        self._emit("run_started")

        self.agent.record({"role": "user", "content": user_message})

        while True:
            # 停机检查
            if self.tool_steps > self.max_steps:
                self.stop_reason = f"tool_steps >= {self.max_steps}"
                ts.stop_step_limit(self.max_steps)
                self._emit("run_finished", {"stop_reason": self.stop_reason})
                self._finalize_run(ts)
                return (
                    "<final>已达到最大工具调用步数限制"
                    f"({self.max_steps})，当前任务未完成。</final>"
                )

            if self.attempts >= self.max_steps * 3 + 4:
                self.stop_reason = f"attempts >= {self.max_steps * 3 + 4}"
                ts.stop_retry_limit(self.max_steps * 3 + 4)
                self._emit("run_finished", {"stop_reason": self.stop_reason})
                self._finalize_run(ts)
                return (
                    "<final>模型输出格式错误次数过多，已终止。"
                    "请检查 System Prompt 中的工具调用格式说明。</final>"
                )

            # 1. 组装 prompt
            prompt_text = self.agent.prompt(user_message)

            # 2. 调用模型（附 cache key）
            self.attempts += 1
            cache_key = getattr(self.agent._prefix, "hash", "")
            raw = self.agent.model_client.complete(
                prompt_text,
                max_new_tokens=self.agent.config.max_new_tokens,
                prompt_cache_key=cache_key,
            )

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
                self.tool_steps += 1
                ts.record_tool(tool_name)

                self.agent.record({
                    "role": "assistant",
                    "content": f"调用工具: {tool_name}",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                })

                result = self.agent.execute_tool(tool_name, tool_args)
                result_text = (
                    result.content if hasattr(result, 'content') else str(result)
                )
                self.agent.update_memory_after_tool(tool_name, tool_args, result_text)
                self.agent.record({"role": "tool", "content": result_text})
                self._emit("tool_executed", {"tool": tool_name})

                if callback:
                    callback.on_tool_executed(tool_name, result_text)

                user_message = (
                    f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"
                )

            elif kind == "retry":
                self.agent.record({"role": "system", "content": str(payload)})
                user_message = str(payload)

    def _emit(self, event: str, payload: dict | None = None):
        """发送 trace 事件到 RunStore。"""
        from agent_runtime.run_store import RunStore

        try:
            store = RunStore(root=self.agent._cwd)
            if self._task_state:
                store.append_trace(self._task_state, event, payload)
        except Exception:
            pass

    def _finalize_run(self, ts):
        """完成 run：写入 task_state + report + checkpoint。"""
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.run_store import RunStore

        try:
            store = RunStore(root=self.agent._cwd)
            store.write_task_state(ts)
            store.write_report(ts, {
                "run_id": ts.run_id,
                "tool_steps": ts.tool_steps,
                "attempts": ts.attempts,
                "stop_reason": ts.stop_reason,
                "status": ts.status,
            })
            create_checkpoint(
                self.agent, ts,
                ts.user_request, trigger="ask_end",
            )
        except Exception:
            pass
