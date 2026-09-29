# FixLoop Subagent 并行探索与证据汇总 MVP Spec

日期：2026-09-30。状态：开发规格；功能尚未实现。

## 1. 目标、交付与前提

让一条真实 L2 修复路径中的主 Agent 把**实现位置定位**与**相关测试发现**作为最多两个互相独立的只读子任务委派出去。Subagent 在各自隔离的 Agent 上下文和工具权限下并行探索，返回带 Observation、路径范围、内容版本和未确认事项的结构化结果。主 Agent 通过稳定句柄回收并核验证据，再决定是否更新 Plan 和修改代码。

必须交付：固定两类探索、批量委派与句柄回收、真实两路 Subagent 模型/工具循环、严格只读权限、预算与取消、证据 freshness、确定性去重/待复核标记、Turn 进度和安全恢复。预计在前置契约已落地后单人 **10–16 个有效工作日**，含相关测试和集成返工；约 1,000–1,700 行实现加测试用于估算，不作为验收指标。

本版主要接入此前 [Plan DAG 完整在途恢复 MVP](./2026-09-30-plan-dag-inflight-resume-mvp.md) 的任务/节点引用、[长任务上下文与证据 MVP](./2026-09-30-long-task-context-evidence-mvp.md) 的 Observation freshness，以及 [Turn 内进度 MVP](./2026-09-30-tool-batch-turn-progress-mvp.md) 的事件投影。它们目前仍是规格文档；未落地时可实现独立协作模块，但不得宣称 Plan 更新、证据 freshness 或 Turn 进度端到端完成。文件/文本工具足以起步；[代码探索 MVP](./2026-09-29-code-exploration-mvp.md) 的 LSP/关系视图只有实际落地且满足只读取消策略时才可选接入。

## 2. 非目标和当前基础

首期不支持任意子任务 DAG、递归委派、动态无限 Subagent、子 Agent 写入/执行测试/终端命令、跨任务复用、语义相似度去重、自动冲突裁决、独立 Planner 或通用分布式 worker 平台。“发现相关测试文件”不等于测试已运行、覆盖了目标符号或通过。

现有 `AgentTask` 包含 run/parent/dependency/budget/lease，`CollaborationStore` 可持久化任务和事件，`TaskDAG` 可验证依赖，`BudgetLedger` 有线程安全预留，Blackboard 有带 evidence refs 的提议/CAS。现有 `TaskScheduler.run_once` 是同步单 handler；`RepairCollaborationRuntime` 只是把 context/patch/verify 阶段映射到任务记录，不执行探索型 Subagent。`role_projection` 面向 critic/verifier，不能直接作为 explorer 的最小上下文或权限证明。`BudgetLedger` 的预留只在内存，恢复不能仅凭该对象断言预算正确。

Batch 提交要避开当前 `TaskScheduler.submit` 的“先 create_task、再 DAG.add”顺序：非法依赖可能留下已持久化任务。本版先对全批 ID/身份/数量/种类/预算/工具能力校验，再在一个事务中创建，不允许部分提交。现有 Blackboard 旧的 `highest_confidence` 仲裁不用于 Subagent 发现；相互不一致的发现须待主 Agent 核验。

## 3. 委派工具与数据契约

仅在受信任的 L2 主 Agent 工具注册表新增：

```text
delegate_exploration(tasks: [{kind, question, scope_paths?, input_observation_ids?}])
collect_exploration(handles: [task_id], wait_ms?: int)
```

一次 `delegate_exploration` 接受 1–2 项，kind 只允许 `implementation_location` 和 `related_tests`，同批各 kind 最多一项，不接受模型传入命令、角色、工具名单、预算、worker ID、Plan revision 或 workspace 根路径。真实值由可信 runtime 填入。返回稳定 task_id/handle、初始状态与 parent turn/Plan node 引用。`collect_exploration` 只接受同一父 task/run 的句柄，等待有上限；超时返回当前状态和已确认的部分结果，不伪造失败、不重复创建任务。重复 collect 幂等，主 Agent 汇总只做一次。

固定 `ExplorationTask`/扩展 AgentTask payload：

```text
schema_version="1", task_id, parent_task_id, run_id, turn_id
workspace_id, workspace_revision, plan_id?, plan_version?, node_id?
kind, question, scope_paths[], input_observation_ids[]
deadline_at, max_model_turns, max_tool_calls, token_reservation
attempt_id, lease_generation, status, result_ref?, error_code?
```

初始参数与输入 Observation 先按 workspace/task scope、checksum、freshness 验证。`scope_paths` 必须经现有路径/敏感目录策略规范化；空 scope 表示仓库根下受限探索，不代表可越权读取。提交整批时绑定父 run 的当前 Plan revision；Plan 改版或工作区修改后，迟到结果仍保存审计但需重验版本，不能直接用于新计划。

## 4. 独立上下文、权限与资源限额

新增专用 `create_explorer_agent`，每个 task 单独实例化 Agent/session/ToolContext、取消 token 和模型调用循环。主 Agent 只传委派目标、固定任务说明、必要 Plan 节点投影、经过校验的输入 Observation 摘要/引用和 workspace 范围；不得复制完整对话、私有草稿、其他子任务结果。Subagent 调用的模型和工具都记录 task/attempt 身份。

工具注册表采用固定 allowlist，例如 `list_files`、`grep`、`read_file`；实际名单在 P0 审计并锁定。注册表和 ToolExecutor/Gateway 双层都拒绝 `write_file`、补丁、`run_shell`、`quick_test`、测试执行、网络调用及能间接执行仓库代码的工具。模型参数不能降低副作用类别。只读工具若有子进程，需确认取消/超时收据；不安全工具不进入 allowlist。子任务无权直接修改主 Plan、Blackboard 的权威 namespace 或主 Agent 任务状态。主 Agent 修改工作区前，活动 Subagent 必须完成/取消并确认退出，或其结果随后按旧版本拒绝。

每任务固定最多 3 次模型调用、4 次只读工具调用、受信任 token 上限与执行 deadline；值可由配置降低，模型不能提高。最多两任务并发，与 Plan 只读探索和 Tool Batch 共用全局只读并发/模型预算门禁，不允许各自独立开两路。批量提交先为总量预留预算；完成按可信 usage/收据结算，失败/取消释放并发名额。恢复时从任务/attempt 收据重建预算；usage 缺失时保守计入已预留上限，不能因为内存 Ledger 丢失而重复花费预算。

## 5. 调度、句柄生命周期、取消与恢复

复用 `CollaborationStore` 的 durable task/lease/CAS，新增只处理该 batch 的两路 worker 池。不能把共享 `TaskScheduler.dag` 无锁给两个 `run_once` 使用；批次验证和持久化、领取任务、结果提交分别有明确事务/版本门禁。任务只有在前置校验成功后才能入队；本版两个任务互不依赖，任一失败不取消另一项。

状态至少：`queued | running | completed | partial | failed | cancelled | timed_out | worker_lost | stale`。只有 `completed` 且证据有效可进入成功发现视图。失败、超时或取消可以保留部分 Observation，但标 `partial`，不可冒充完整搜索。`collect` 返回任务当前状态及可校验 result_ref；重复等待/回收不再次扣预算、不再次合并。

父任务取消时先停止新派发，向两个子 Agent/每次只读工具传播取消，限时等待；单独取消句柄的能力可由可信控制接口提供，但本版不向模型开放任意取消策略。线程池 `shutdown(wait=False)` 不构成终止确认。worker 未退出时报告 `worker_lost`/清理未确认，后续迟到结果须由 task_id + attempt_id + lease_generation 拒绝写入当前结果。子任务只读，不自动重跑写入；外部 `rg` 等进程若清理未确认，遵循既有资源协调的 `recovery_required`。

checkpoint 保存父 run、task IDs、attempt/lease generation、预算 reservation/结算引用、result/Observation refs、Plan revision 和事件水位；不保存活 Agent 或线程。恢复时先由父 run 恢复器确认旧 worker 停止/隔离，再对 running/worker_lost 的只读任务产生新 attempt；已完成且 scope/版本/收据仍有效的结果可复用。旧 Plan 或文件版本不符时结果标 stale，交主 Agent 重取/核验。任何失效旧 worker 的迟到提交不能覆盖新 attempt。

## 6. 结果、证据和汇总

`ExplorationResult(schema_version="1")` 至少包含 task/parent/run/workspace/Plan 身份、attempt、状态、摘要、`findings[]`、`unknowns[]`、搜索范围、完整性、usage、起止时间及错误码。每条 finding：`claim_key`、陈述、类别、仓库相对路径/范围、来源 Observation ID、来源工具、完整文件 hash、`direct | parsed | candidate` 解析状态。空结果也记录已扫描范围、截断和限制；不能提升为“仓库不存在”。结果元数据持久化在 CollaborationStore，较长原始工具输出继续放 ObservationStore，仅传引用和短摘要。

主 Agent 收集时重验 task/run/workspace/Plan 作用域、Observation checksum、文件 hash 与完整性。仅对相同 `claim_key + path + range + file_hash + scope` 的**确定性完全重复**发现合并来源；版本或范围不同的发现分别保留。相同目标键但陈述相反/不一致时只标 `needs_review` 并列出双方证据，不做自然语言语义判决或按投票/置信度选赢家。主 Agent 复核后才可通过 Plan reducer/决策记录接纳发现；Subagent 结果本身只是候选证据。Blackboard 可用受限 proposal/CAS 存候选，但不让子任务直接写权威状态。

## 7. Turn 进度、事件和验收

复用既有 Canonical Trace 与 Turn 进度投影。事件：`exploration_batch_submitted/rejected`、`subagent_queued/started/partial/completed/failed/timed_out`、`subagent_cancel_requested/cleanup_checked`、`subagent_result_collected`、`subagent_evidence_stale`、`subagent_review_required`。每项含 run/parent_turn/task/attempt/Plan revision、事件序号、状态、预算与耗时、短摘要；不记录完整 prompt、源码、模型内部推理或敏感输出。事件用于 UI，CollaborationStore/Observation/Plan 才是恢复依据。

| ID | 场景 | 必须观察到的行为 |
|---|---|---|
| A1 | 主 Agent 一次委派两类独立探索 | 两个真实 Subagent Agent 循环并行；句柄稳定，collect 可等待并幂等回收。 |
| A2 | 子任务请求写/补丁/shell/quick_test | 注册与执行闸口拒绝，工作区无修改、测试未运行。 |
| A3 | 重复 kind、超 2 项、跨 run 句柄、越权 scope、无效输入证据 | 提交/回收拒绝，无半批任务或权限提升。 |
| A4 | 预算/并发到限、一个子任务失败 | 上限有效；另一独立任务继续；预算释放/结算有收据。 |
| A5 | 同一发现、不同文件版本、相反发现 | 仅完全重复合并来源；不同版本分别保留；矛盾标 needs_review。 |
| A6 | 文件修改、结果截断、无结果 | 旧结果 stale/partial/unknown，不当作已确认代码事实或测试覆盖。 |
| A7 | 父取消、worker 丢失、旧 worker 迟到 | 停止新派发；清理状态准确；旧 attempt 不覆盖新结果。 |
| A8 | checkpoint 恢复 | 已完成新鲜结果可复用；运行中任务先确认旧执行结束再新 attempt；预算不重复扣减。 |
| A9 | Turn 进度重放 | 事件能重建状态且不重复计数，不泄漏原始内容。 |
| A10 | 真实 L2 修复任务 | 主 Agent 用核验证据更新 Plan，随后由主 Agent 独自修改和验证；有实际模型/tool/Observation/Plan 记录。 |

A1 需用同步屏障或可控慢工具证明重叠，不能只靠完成耗时猜测。A10 不能用纯 mock 的 `AgentResult` 充数。固定任务记录实际定位覆盖、证据有效率、额外 token/工具调用、重复工作、并发峰值和耗时；只报告实测数据，不预设改善。相关测试与 lint/format 按 `CLAUDE.md` 执行；全量测试须用户显式授权。配套 [开发计划](../plans/2026-09-30-subagent-readonly-exploration-mvp.md)。
