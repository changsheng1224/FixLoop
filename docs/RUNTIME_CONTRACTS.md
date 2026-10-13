# 通用运行时契约

Layer 1 的 `Agent` 不依赖修复业务。模型调用、协议解析、工具分派、重试、预算和取消集中在同一执行核心；角色行为通过系统提示、工具治理、loop policy 和 context policy 注入。Layer 2 的正式接口见 [修复契约](REPAIR_CONTRACT.md)。

```python
from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext

config = AgentConfig(
    provider="fake", max_model_attempts=10,
    budget={"max_llm_calls": 10, "max_tool_calls": 20, "total_tokens": 24000},
    deadline={"step_s": 120, "tool_s": 30},
)
agent = Agent(config, client, WorkspaceContext.build(repo_path), cwd=repo_path)
answer = agent.ask("读取文件并说明问题")
```

应用调用工具使用 `Agent.execute_tool`，闸口使用 `ToolExecutor.execute_gated`。工具结果为 `ToolResult`，工具 schema 与治理注册表一致。只读权限通过工具白名单／治理策略限制，工具配额由有效配置生成；不以修复角色名称在运行时分配权限。

`AgentConfig.snapshot()` 在执行前提供有效配置和来源。`prompt_budget/hard_cap/model_context_window` 控制单次上下文；`budget` 控制累计资源；`deadline` 控制时间。`max_model_attempts` 明确限制 XML 调用尝试，`protocol_recovery_turns` 和 `max_empty_attempts` 控制协议恢复。没有工厂隐藏上限。

取消令牌、预算和总截止时间贯穿模型、工具与重试等待。`Retry-After` 不缩短；不足时间等待时明确失败。Explorer 使用同一核心和隔离上下文策略；普通聊天与项目任务调度器独立。

统一纯文本补丁核心严格检查原文和原始 hunk 坐标，后端承担权限、快照与原子写入。最终回答不会触发补丁应用。所有支持和拒绝格式见 [补丁契约](REPAIR_CONTRACT.md#补丁核心和后端事务)。

RuntimePolicy 为 `2.0`，session 与 checkpoint envelope 为 `3.0`。旧字段、旧导出和旧 checkpoint 拒绝，不自动迁移。
