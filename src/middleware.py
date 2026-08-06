"""ToolGateway 权限中间件：声明式工具权限控制。

权限规则对 Agent 透明——Agent 收到的是普通工具错误返回，不知道被拦截。
"""

from agent_runtime.tool_executor import ToolExecutionResult
from agent_runtime.tool_rejection import build_gateway_rejection_metadata


class ToolGateway:
    """Agent 工具调用权限网关。"""

    def __init__(
        self,
        *,
        policy=None,
        registry,
    ):
        if policy is None:
            from src.collaboration_governance import CollaborationGovernance

            policy = CollaborationGovernance(registry=registry)
        self._registry = registry
        self._policy = policy
        self._runtime_context: dict[str, dict[str, str]] = {}

    def set_context(
        self,
        agent_name: str,
        *,
        mode: str = "repair",
        phase: str = "",
        evidence: bool = True,
        read_before_write: bool = True,
    ) -> None:
        self._runtime_context[agent_name] = {
            "mode": mode,
            "phase": phase,
            "evidence": evidence,
            "read_before_write": read_before_write,
        }

    def bind_tools(self, tools: dict[str, dict]) -> None:
        """Register runtime-provided tools in the canonical registry."""
        self._registry.bind_execution_tools(tools)
        self._policy.refresh_from_registry(self._registry)

    def query_capabilities(
        self,
        agent_name: str,
        *,
        phase: str = "",
        mode: str = "repair",
    ) -> dict:
        """Return the same governed capability view used by authorization."""
        return self._registry.capabilities_for(agent_name, phase=phase, mode=mode)

    def can_call(self, agent_name: str, tool_name: str) -> bool:
        """检查 agent 是否被授权调用 tool。"""
        context = self._runtime_context.get(agent_name, {})
        return any(
            spec.name == tool_name
            for spec in self._registry.visible_to(
                agent_name,
                context.get("phase", ""),
                context.get("mode", "repair"),
            )
        )

    def dispatch(self, agent_name: str, tool_name: str, execute_fn):
        """有权限则执行 execute_fn，否则返回 permission_denied 工具结果。"""
        if not self.can_call(agent_name, tool_name):
            return ToolExecutionResult(
                content=f"Error: 工具 '{tool_name}' 对 '{agent_name}' 不可用。",
                metadata=build_gateway_rejection_metadata(),
            )
        if self._policy is not None and agent_name in self._runtime_context:
            context = self._runtime_context.get(agent_name, {})
            decision = self._policy.authorize(
                tool_name,
                role=agent_name,
                mode=context.get("mode", "repair"),
                phase=context.get("phase", ""),
                evidence=bool(context.get("evidence", False)),
                read_before_write=bool(context.get("read_before_write", False)),
            )
            if not decision.allowed:
                return ToolExecutionResult(
                    content=(
                        f"Error: {tool_name} rejected: {decision.reason}; "
                        f"alternatives={decision.alternatives}"
                    ),
                    metadata=build_gateway_rejection_metadata(
                        rejection_reason=decision.reason,
                        policy_reason=decision.reason,
                        alternatives=decision.alternatives,
                    ),
                )
        return execute_fn()

def build_repair_gateway(repo_root: str = "") -> ToolGateway:
    """返回 Layer 2 修复流水线默认 ToolGateway。

    若 ``.agent/tools.yaml`` 存在，直接覆盖对应 ToolSpec 的角色集合。
    """
    from src.collaboration_governance import CollaborationGovernance
    from src.tools.spec import default_repair_tool_registry

    registry = default_repair_tool_registry()
    if repo_root:
        from src.tools.manifest import load_tool_role_overrides

        for name, roles in load_tool_role_overrides(repo_root).items():
            registry.set_roles(name, roles)
    return ToolGateway(
        policy=CollaborationGovernance(registry=registry),
        registry=registry,
    )
