# 当前运行时契约

运行时预算、超时、下述工具入口和修复状态统一使用当前契约，不提供历史别名或自动迁移。

## 配置

预算放在 `budget`，超时放在 `deadline`。Python 构造和 JSON 配置使用相同结构：

```python
from agent_runtime import AgentConfig

config = AgentConfig(
    budget={"max_llm_calls": 20, "max_tool_calls": 40, "max_write_calls": 8},
    deadline={"repair_s": 600, "step_s": 120, "tool_s": 30},
)
```

```json
{
  "budget": {"max_llm_calls": 20, "max_tool_calls": 40, "max_write_calls": 8},
  "deadline": {"repair_s": 600, "step_s": 120, "tool_s": 30}
}
```

环境变量使用 `FIXLOOP_BUDGET_MAX_LLM_CALLS`、`FIXLOOP_BUDGET_MAX_TOOL_CALLS`、
`FIXLOOP_DEADLINE_REPAIR_S`、`FIXLOOP_DEADLINE_STEP_S`、`FIXLOOP_DEADLINE_TOOL_S`。
CLI 的 `--tool-timeout` 和 `--step-timeout` 直接设置嵌套超时字段。

原标量字段 `max_llm_calls_per_repair`、`max_tool_calls`、`max_write_calls`、
`max_verify_calls`、`max_recovery_attempts`、`repair_wall_timeout_s`、
`step_timeout_s`、`tool_timeout_s` 已删除，配置中出现这些字段会报错。
旧标量环境变量不再读取。限额和超时中的 `0` 表示不限制。

`prompt_budget` 是单次请求上下文上限，`budget.prompt_tokens` 是整次运行累计预算，
两者分别控制不同资源。

## 工具和导入

- 搜索工具统一叫 `grep`，参数类型是 `GrepArgs`，不再注册 `search`。
- 工具执行结果统一使用 `agent_runtime.tool_result.ToolResult`。
- 应用调用工具通过 `Agent.execute_tool()`；闸口直接调用使用
  `ToolExecutor.execute_gated()`，不再提供 `ToolExecutor.execute()`。
- 回调基类是 `agent_runtime.callbacks.AgentCallback`。
- 上下文预算适配函数从 `agent_runtime.context_fit` 导入。
- 任务保护与任务模板函数从 `agent_runtime.task_section` 导入；通用模板渲染从
  `agent_runtime.template_render` 导入。

Patcher 通过受治理工具修改磁盘，运行时根据快照差异生成 `CandidatePatch`，用于
审计和导出。最终回答中的 JSON 或文本 diff 不会被当成落盘补丁重新应用。

## 状态和恢复

`RepairState` 只读取显式标注 `schema_version: "1.2"` 的状态。
旧版本、缺失版本、旧阶段 `retrieve`、旧状态 `patched` 均被拒绝；上下文阶段使用
`context`，已生成但未验证的补丁使用 `pending_verify`，验证成功使用 `fixed`。

L1 和 L2 checkpoint 必须携带当前版本 `2.0` 的 `CheckpointEnvelope`，并通过完整性
校验。历史无 envelope 的 checkpoint 和 L2 `1.0`/`1.1` 状态不能继续恢复，需创建新运行。
现有历史结果文件仍可留作人工审计，不会被自动改写。
