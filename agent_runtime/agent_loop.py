"""Agent 控制循环：感知 → 决策 → 行动 → 记录 → 循环。

每个循环周期：
1. 组装 prompt（prefix + 历史 + 当前请求）
2. 调用模型获取响应
3. 解析响应为 tool / final / retry
4. tool → 执行工具并记录结果 → 继续循环
5. final → 记录并返回答案
6. retry → 记录并通知模型重试

停机条件：
- 模型返回 <final> → 正常结束
- tool_steps >= max_steps → 步数耗尽
- attempts >= max_steps * 3 + 4 → 格式错误过多
"""


class AgentLoop:
    """Agent 控制循环。

    管理对话回合的循环执行，统计步数和尝试次数。
    """

    def __init__(self, agent, max_steps: int | None = None):
        self.agent = agent
        self.max_steps = max_steps or agent.config.max_steps
        self.tool_steps = 0
        self.attempts = 0
        self.stop_reason = ""

    def run(self, user_message: str) -> str:
        """执行控制循环，返回最终答案。

        Args:
            user_message: 用户输入。

        Returns:
            最终答案文本。
        """
        # 记录用户输入
        self.agent.record({"role": "user", "content": user_message})

        while True:
            # 停机检查
            if self.tool_steps > self.max_steps:
                self.stop_reason = f"tool_steps >= {self.max_steps}"
                return (
                    "<final>已达到最大工具调用步数限制"
                    f"({self.max_steps})，当前任务未完成。</final>"
                )

            if self.attempts >= self.max_steps * 3 + 4:
                self.stop_reason = f"attempts >= {self.max_steps * 3 + 4}"
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
                return str(payload)

            elif kind == "tool":
                tool_name = payload.get("name", "unknown")
                tool_args = payload.get("args", {})
                self.tool_steps += 1

                # 记录工具调用
                self.agent.record({
                    "role": "assistant",
                    "content": f"调用工具: {tool_name}",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                })

                # 执行工具（经 ToolExecutor 闸口）
                result = self.agent.execute_tool(tool_name, tool_args)
                # 处理 ToolExecutionResult
                if hasattr(result, 'content'):
                    result_text = result.content
                else:
                    result_text = str(result)
                self.agent.record({"role": "tool", "content": result_text})

                # 更新 user_message 为工具结果反馈
                user_message = (
                    f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"
                )

            elif kind == "retry":
                # 通知模型格式错误，不增加 tool_steps
                self.agent.record({"role": "system", "content": str(payload)})
                user_message = str(payload)
