# 状态和配置契约

正式流程及全部状态含义见 [修复契约](REPAIR_CONTRACT.md)。当前 `RepairState`、`VerificationResult`、`RepairConfig` 和 `RuntimePolicy` 版本为 `2.0`；session／checkpoint envelope 为 `3.0`；Workbench schema 为 `3`。缺失或旧版本拒绝，不迁移。

`RepairState.control` 保存取消、超时、编辑范围、验证和恢复控制；`execution` 保存执行身份、checkpoint 和收据；`node_timings` 只保存诊断、耗时和事件证据。`fixed` 要求达到请求规定的真实验证条件，`pending_verify` 代表已修改但未验证。资源清理／回滚未知时为 `recovery_required`。

配置统一解析优先级：默认值 → profile → 用户文件 → 工作区文件 → 环境变量 → 显式参数。用户文件为 `~/.fixloop/config.json`；工作区依次读取 `.fixloop/config.json` 和 `.agent/config.json`。工作区不能激活外部代码探索服务。未知字段、非法值及旧标量配置均报错。

工作区也不能配置外部网页服务：`web` 段仅接受宿主／用户／显式请求来源。
`web.enabled` 默认 false，`budget.max_web_requests` 默认 8（含重试，0 表示不限额）；
详见 [外部资料搜索与抓取](WEB_RESEARCH.md)。

```json
{
  "schema_version": "2.0",
  "prompt_budget": 8000,
  "hard_cap": 8000,
  "max_model_attempts": 30,
  "budget": {"total_tokens": 100000, "max_tool_calls": 24, "max_write_calls": 8},
  "deadline": {"repair_s": 300, "tool_s": 60},
  "repair": {
    "schema_version": "2.0",
    "sandbox_policy": "preferred",
    "exploration": {"model_turns": 2, "tool_calls": 3, "tokens": 12000}
  }
}
```

`snapshot()` 返回 `values`、`provenance`、`config_hash`。单次上下文与累计 token、工具调用和时间限制独立。预算／deadline 的 `0` 表示不限额；安全边界在配置模型中声明，超限拒绝。

环境覆盖如 `FIXLOOP_BUDGET_TOTAL_TOKENS`、`FIXLOOP_BUDGET_MAX_TOOL_CALLS`、`FIXLOOP_MAX_MODEL_ATTEMPTS`、`FIXLOOP_DEADLINE_REPAIR_S`。修复业务默认值集中在修复层。CLI、Workbench 和评测只调用 `RepairService.run(request)`，没有旧入口或配置兼容别名。
