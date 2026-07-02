"""ToolGateway 权限中间件：声明式工具权限控制。

权限规则对 Agent 透明——Agent 收到的是普通工具错误返回，不知道被拦截。
"""


class ToolGateway:
    """Agent 工具调用权限网关。

    权限表格式：{tool_name: {allowed_agent_names}}

    特殊值：
    - "*" 表示所有 Agent 可调用
    - 不在表中的工具默认拒绝
    """

    def __init__(self, permission_table: dict[str, set[str]]):
        self._table = {}
        for tool, agents in permission_table.items():
            self._table[tool] = agents if isinstance(agents, set) else set(agents)

    def can_call(self, agent_name: str, tool_name: str) -> bool:
        """检查 Agent 是否有权调用工具。"""
        allowed = self._table.get(tool_name)
        if allowed is None:
            return False
        return "*" in allowed or agent_name in allowed

    def dispatch(self, agent_name: str, tool_name: str, execute_fn):
        """执行工具调用（经权限检查）。

        Args:
            agent_name: 发起调用的 Agent 名。
            tool_name: 工具名。
            execute_fn: 无参执行函数（仅权限通过后才调）。

        Returns:
            执行结果或 permission_denied 结果。
        """
        if not self.can_call(agent_name, tool_name):
            from agent_runtime.tool_executor import ToolExecutionResult

            return ToolExecutionResult(
                content=f"Error: 工具 '{tool_name}' 对 '{agent_name}' 不可用。",
                metadata={
                    "tool_status": "rejected",
                    "tool_error_code": "permission_denied",
                },
            )
        return execute_fn()

    def grant(self, agent_name: str, tool_name: str):
        """动态授权。"""
        if tool_name not in self._table:
            self._table[tool_name] = set()
        self._table[tool_name].add(agent_name)

    def revoke(self, agent_name: str, tool_name: str):
        """动态撤销。"""
        if tool_name in self._table and agent_name in self._table[tool_name]:
            self._table[tool_name].discard(agent_name)
