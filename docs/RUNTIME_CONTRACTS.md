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


## 2026-10-04：工具契约破坏式更新

- 通用 `ToolSpec`、`ToolRegistry` 与执行投影从 `agent_runtime.tool_spec` 导入。
  `src.tools.spec` 只声明修复域默认权限；不再提供旧路径兼容导出。
- 注册表只接受 `schema` 字段中的 object JSON Schema。删除简写参数格式、
  `json_schema` / `protocol_schema` 双轨字段与隐式类型转换。
  `auto_schema()` 从 dataclass 生成 JSON Schema，保留 nullable、数组项类型和默认值。
- 所有工具执行函数返回 `ToolResult`。文本消费者显式读取 `.content`；
  执行器检查 `.status`、`.error_code`、`.retryable`，不再根据 `Error` 文本猜测失败。
  自定义工具返回裸字符串会被拒绝为 `invalid_tool_result`。
- L2 通过 `ToolContext.edit_lock` 注入 `EditPolicy`，通过 `grounding_sink` 接收证据。
  删除按仓库路径保存的全局编辑锁注册表及静默异常放行。
  恢复检查点只恢复编辑范围，必须重新读取文件才能写入。
- `IssueIntentAdapter` 移至 `src.repair.intent_adapter`。L1 不再导入 L2；
  `tests/test_runtime_contract_boundaries.py` 以 AST 检查保护依赖方向。

自定义工具、外部调用方与测试桩必须直接改用以上契约，不提供自动迁移层。
Skill 注册、上下文装配和持久化格式的其余兼容路径于 2026-10-05 一并收敛，见下文。

## 2026-10-04：结果、校验与进程执行收敛

- `ToolResult` 的 `status`、`error_code`、`retryable`、`changed_files`、
  `receipt`、`duration_ms`、`output_truncated` 是运行时唯一状态来源。
  `metadata` 只保存扩展信息，写入重复控制字段会被拒绝。
  日志和持久化使用 `to_metadata()` 导出的独立快照；不支持旧 metadata 构造方式。
  工具执行后的策略判定完成后重新生成回执，再写入 action ledger 和 Observation。
- 工具参数采用 JSON Schema Draft 2020-12，由 `jsonschema` 完整校验；
  不进行类型转换。支持组合约束、布尔 schema 和本地 `$ref`，禁用远程引用加载。
  保留标准的开放对象语义；需要禁止未知字段的工具显式声明
  `additionalProperties: false`。MCP 发现、模型投影和执行校验保留完整 schema。
- 宿主 shell 使用统一执行器，持续读取 stdout/stderr，按流限制保留的输出，
  统一使用 UTF-8 解码和单调时钟计时。取消、超时清理进程组并报告清理是否确认；
  `data.exit_code` 保存命令退出码，`output_truncated` 标记输出截断。
- 声明式验证使用解析后的可执行文件路径；启动时的操作系统错误返回
  `verification_environment_failed`，不再以未捕获异常中断流程。

## 2026-10-05：兼容面收敛（破坏式）

在保持先进实现的前提下删除历史别名、转发壳与死代码，调用方必须改用当前契约。

- **Skill 合同**：删除转发壳 `src.skills.prompt`（直连 `src.skills.skill_block`）；
  `SkillContext.from_dict` 只认当前键，不再反查旧 `skill_*` 键；
  删除未接线的 `SkillKind.HYBRID`；`src.skills` 包不再导出内部符号。
- **上下文运行时**：删除 `ContextPolicyEngine.select`，统一走 `select_with_result`；
  `ObservationStore.put` 不再接受 `redact` 形参；磁盘缓存不再读取旧单行格式。
- **停机与状态**：删除 legacy 自由文本停机归一化
  （`normalize_stop_reason` / `stop_reason_detail_from_legacy`）与未使用的
  `TaskState.stop()`；`TaskState.from_dict` 原样读取 `stop_reason`。
- **记忆**：删除无人调用的 `normalize_memory_state` 与孤立的 `MAX_FILE_SUMMARIES`。
- **预算与 CLI**：删除 trace/prompt 中的 `payload["legacy"]` 旧预算视图；
  删除 CLI 中无生产者的 `{agent}_internal` 计时回退（仅保留 `phases_internal`）。
- **执行杂项**：删除 `PhaseTimeoutConfig.from_repair_timeout`
  （改用 `with_repair_total_cap`）、`patch_applier.extract_json_block`
  （直接调用 `agent_runtime.json_recovery.repair_structured_output`）、
  `swebench.harness._parse_resolved` 与 `UserProfileStore.remove`
  （改用 `invalidate`）。
