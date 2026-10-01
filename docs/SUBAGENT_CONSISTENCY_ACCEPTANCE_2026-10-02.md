# Subagent 一致性 MVP 验收

日期：2026-10-02。范围：用户确认的委派计划视图、子循环预检、可解释收集诊断。

## 交付行为

### 委派上下文

复用 `PlanSession.build_long_task_context()` 生成的同一 `plan_view`，委派只保存目标、全部硬约束、当前节点及完成条件、计划身份/版本/校验引用和任务状态 revision。不复制父对话、节点历史、活动决策、其他节点或整个记忆库。

`delegation_context` 是脱离源对象的只读快照，绑定 parent task/run/workspace；其 checksum 与 Plan/node 身份在 worker 调用 provider 前检查。快照写入既有 task payload，恢复继续使用原快照，attempt/generation 由原恢复流程递增。快照可读不授予节点写入或完成权限。

完整快照上限 8000 UTF-8 字节；超限返回 `exploration_context_budget_exceeded`，在创建任务和预留本批预算前拒绝整批。目标和硬约束不静默截断；既有 token/model-turn/tool/deadline 门禁继续生效。8000 是本地有界策略，不是 tokenizer 测量或上游常量。

独立无 Plan 的 ExplorationRuntime 继续允许空计划上下文；真实 L2 必须绑定有效视图。已有 planned task 若缺少有效快照，不自动迁移或伪造上下文。

### 子循环预检

复用 Layer 1 的 `ToolCallBatch.create()` 和 `validate_native_content()`，只消费调用身份及纯 schema 预检；不接入 Scheduler、并行执行或完整主 AgentLoop。

- native 调用 ID 缺失/重复、未知工具、非法结构、raw/normalized 数量/次序/ID/名称/参数不一致：整批零读取，结果 partial，错误码 `tool_batch_protocol_error`。
- 不为缺失 native ID 生成替代 ID。原始 content 存在时原样保留，含 text block；无原始 content 的既有规范化 adapter 契约沿用。
- schema 错误返回配对拒绝，合法兄弟仍串行执行，执行器继续负责范围、权限和执行预算。
- XML 保持单调用，协议适配时生成单调用 ID，复用参数预检；未增加 XML 批次。
- 子 Agent 固定四次尝试上限仍包含拒绝调用；参数预检不调用 Executor、也不消耗其执行 quota。这与主 Agent 的预算口径不同，属于已有子任务保守限额契约。
- 截断输出沿用既有零派发行为。

### 收集诊断

每项返回 `validation: {status, reason}`，status 为 valid/stale/not_applicable；非 completed 结果不宣称证据有效。`diagnostics` 按 validation/execution/cleanup 分类，复用当前 task/result/Observation 状态生成。

| reason | 含义 |
| --- | --- |
| result_checksum_invalid | 持久结果校验不一致 |
| result_scope_mismatch | task/parent/run/workspace/attempt/generation 不一致 |
| result_incomplete | 结果未确认完整 |
| workspace_changed | workspace 快照变化，沿用全工作区失效粒度 |
| plan_changed | 当前 Plan 身份或版本变化 |
| delegation_context_invalid | 委派快照校验或身份错误 |
| task_context_changed | 当前目标或硬约束与委派快照不同 |
| observation_missing / observation_inactive | 来源缺失或已失效 |
| observation_scope_mismatch / observation_incomplete | 来源身份不一致或不完整 |
| observation_checksum_invalid / observation_version_changed | blob/来源校验或文件版本不一致 |
| retrieval_incomplete_or_rejected | 子循环读取不完整或被拒绝 |
| claim_observation_not_owned 等 | 保留结构化结果契约的稳定错误码 |
| exploration_cleanup_unconfirmed | worker 清理尚未确认，写入门禁继续阻断 |

freshness 返回检查顺序中的首个原因，不保证列出所有同时失效项。无关审计事实增加 task revision 不使结果失效；目标/硬约束变化会使结果失效。collect 投影不修改原始结果 checksum，重复收集仍沿用已有事件幂等与预算结算。

发现继续是 candidate；过期/partial 结果不进入合并 findings。owner 重读继续只产生 source_review 审计事实，不自动接纳陈述、生成语义决策、重新委派、重规划或改变 Plan。

## 模块与验证

- [委派投影](../src/collaboration/exploration_projection.py)：有界快照与引用校验。
- [探索运行时](../src/collaboration/exploration_runtime.py)：委派/恢复接线、freshness 原因。
- [子循环](../src/agents/explorer.py)：协议/schema 预检、原序回复与稳定错误码。
- [结果消费](../src/collaboration/exploration_results.py)：诊断投影和契约错误码。
- [新增测试](../tests/test_subagent_consistency.py)、[已有运行时](../tests/test_exploration_runtime.py)、[L2 验证](../tests/test_exploration_l2.py)。

相关测试节点去重 **69 passed**，其中新增 **26** 个。首轮 65 passed、1 failed（新清理测试缺少 threading 导入）；修正后精确复验失败节点，以及增加断言的 XML/native L2 节点。随后只补跑三个新增节点和增加诊断断言的 Plan 变化节点，没有重复整批或运行全量测试。

实际行为验证：

1. native 错配/身份/结构/未知工具整批零读取；坏参数与合法兄弟配对，原始 content 保留；XML 单调用成功/拒绝。
2. 主 Agent 的 XML/native 两条 L2 链路均委派两个子任务；子请求实际消费 edit 节点目标和完成条件。owner 重读后唯一写入，真实 host pytest 各通过一个测试。
3. 真正子进程 `os._exit(74)` 后恢复：同 task handle、相同委派快照、新 attempt，旧调用预留上限保守计账；新结果通过当前来源校验。
4. 清理未确认时 collect 明确诊断、before_write 阻断；worker 实际退出后显示 cancelled + cleanup_confirmed。

受控模型用于稳定验证协议和运行时，未访问在线模型，不据此宣称成本、性能或修复率提升。首轮出现既有 `.pytest_cache` 写权限 warning，后续精确复验禁用 cacheprovider；未改权限。

保留的 L2 与恢复记录见 [产物摘要](../artifacts/subagent-consistency-mvp-2026-10-02/summary.json)。受影响七个 Python 文件 Ruff check/format 通过；文档链接和差异空白检查通过。Git 保留既有工作区，不提交、推送或创建 PR。

## 借鉴与取舍

沿用 [五维参照](AGENT_DESIGN_REFERENCES_2026-10-01.md) 的来源记录，本轮不重新作在线上游审计。

- OpenCode R-01/R-06：角色、权限与聚焦探索边界；保留最多两个只读任务，不复制默认权限配置。
- Pi R-02/R-05：上下文投影与执行边界，复用已有纯预检；不引入 TS 内核、第二个调度器或子工具并行。
- Claude R-07：独立子上下文与最小必要任务信息；完整目标和约束在有界快照内保留。
- Hermes R-08/R-09：历史按需检索、经验候选继续后置，不为本轮增加模型调用或持久层。

字节上限、诊断码、全工作区 freshness 和 generation/receipt 门禁为 FixLoop 本地设计。递归委派、写 Agent、自动重试、通用协作 DAG、语义自动接纳与更多上下文入口迁移均不属于本轮。
