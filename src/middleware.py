"""ToolGateway 权限中间件：声明式工具权限控制。

权限规则对 Agent 透明——Agent 收到的是普通工具错误返回，不知道被拦截。
"""


class ToolGateway:
    """Agent 工具调用权限网关。"""

    def __init__(self, permission_table: dict[str, set[str]]):
        self._table = {}
        for tool, agents in permission_table.items():
            self._table[tool] = agents if isinstance(agents, set) else set(agents)

    def can_call(self, agent_name: str, tool_name: str) -> bool:
        """检查 agent 是否被授权调用 tool。"""
        allowed = self._table.get(tool_name)
        if allowed is None:
            return False
        return "*" in allowed or agent_name in allowed

    def dispatch(self, agent_name: str, tool_name: str, execute_fn):
        """有权限则执行 execute_fn，否则返回 permission_denied 工具结果。"""
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
        """为 agent 追加 tool 调用权限。"""
        if tool_name not in self._table:
            self._table[tool_name] = set()
        self._table[tool_name].add(agent_name)

    def revoke(self, agent_name: str, tool_name: str):
        """撤销 agent 对 tool 的调用权限。"""
        if tool_name in self._table and agent_name in self._table[tool_name]:
            self._table[tool_name].discard(agent_name)

    def wrap_agent(self, agent_name: str, agent):
        """包裹 Agent 的 execute_tool，注入权限检查。"""
        original = agent.execute_tool
        gw = self

        def guarded(name, args):
            return gw.dispatch(agent_name, name, lambda: original(name, args))

        agent.execute_tool = guarded


# ---- 共享权限表 ----

REPAIR_PERMISSION_TABLE = {
    "ast_parse": {"localizer"},
    "stack_parse": {"localizer"},
    "write_file": {"patcher"},
    "patch_file": {"patcher"},
    "git_blame": {"localizer", "retriever", "patcher"},
    "git_diff": {"localizer", "retriever", "patcher"},
    "find_test": {"retriever", "patcher"},
    "search": {"*"},
    "read_file": {"*"},
    "list_files": {"*"},
    "*": {"*"},  # 其余工具所有 Agent 可用
}


def build_repair_gateway() -> ToolGateway:
    """返回 Layer 2 修复流水线默认 ToolGateway。"""
    return ToolGateway(REPAIR_PERMISSION_TABLE)
