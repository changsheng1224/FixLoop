# FixLoop 长任务上下文与证据管理 MVP Spec

日期：2026-09-30。状态：开发规格；本文不表示功能已实现。

## 1. 目标与交付

在已有 Plan DAG、Observation、ContextPolicyEngine、L0–L5 压缩和 checkpoint 上，建立一条可核验的长任务上下文链路：每次模型调用从当前 Plan 节点组装上下文；原始目标、硬约束和计划状态以结构化状态为准；代码证据在进入上下文或按引用展开前校验版本；恢复后只使用已核验的状态与证据。

必须交付：

1. 任务权威状态和少量关键决策的版本化记录，不靠压缩摘要恢复目标或 Plan。
2. 节点级 ContextRequest、必需信息预算门禁、选择/裁剪与 freshness manifest。
3. 具有明确文件依赖的 Observation 消费前版本校验、写后失效和按需重取状态。
4. checkpoint 封装与恢复校验；重组当前节点上下文，不重放未知副作用。
5. 固定多阶段任务的压缩、修改、恢复和越权引用测试；记录实测数据，不预设提升幅度。

预计在 **Plan DAG MVP 已实现并稳定接入同一条 L2 修复路径后**，单人约 10–15 个有效工作日，含相关测试和集成返工。该估计不包含 Plan DAG 自身的 18–28 日，也不包含代码探索或 WSL 沙箱 MVP。实现与测试规模约 900–1,500 行，仅用于控制范围，不是验收指标。

## 2. 依赖、当前基础与非目标

本功能消费此前的 [Plan DAG 与完整在途任务恢复 MVP](./2026-09-30-plan-dag-inflight-resume-mvp.md) 提供的 PlanSession、plan_version、state_revision、node_id、attempt journal 和恢复判定。该 Plan 当前仍为 spec；其真实接口落地前只允许实现独立模型和测试，不得用现有 `plan_todos` 冒充 DAG 节点，也不得宣称端到端完成。

现有基础：

| 模块 | 复用与缺口 |
|---|---|
| `agent_runtime/context_runtime.py` | ContextRequest、ContextPolicyEngine、ObservationStore 已有预算选择、外置输出、checksum、按路径失效；`hard_pin` 仍会因预算不足而被裁剪。 |
| `agent_runtime/context_manager.py` | 已记录 selected/dropped manifest；当前 state 主要是 Todo 摘要，应改为 Plan 节点投影。 |
| `agent_runtime/compression_pipeline.py` | L0–L5 和首条用户消息保护已存在；结构化任务状态不能由其摘要推断或覆盖。 |
| `agent_runtime/checkpoint.py`、`session_contract.py` | 已有 sealed envelope/context manifest/Observation manifest；当前 Observation manifest 取最近 100 条，不能保证所有活动引用都被封装。 |
| `src/collaboration/contracts.py` | 已有 AgentTask/AgentResult；首期只校验结果状态和证据引用，不新增语义合并器。 |

首期不做：完整负向搜索/目录新增文件的自动失效；跨任务证据缓存；Subagent 语义去重、投票或冲突裁决；新的日志/证据数据库；通用决策知识图谱；全部 L1/L2 入口迁移；大规模开启/关闭效果对照实验。对范围搜索的“未找到”结论，在缺少可校验范围版本时只能标 `unknown`，不得当作“已确认不存在”。

## 3. 权威状态与决策契约

在 PlanSession 所属 task/run/workspace 下增加 `TaskContextState`，由受信任任务入口初始化：

```text
TaskContextState(schema_version="1"):
  task_id, run_id, workspace_id, session_id
  original_request_ref, original_request_checksum
  hard_constraints[]: {constraint_id, text, source_turn_id}
  plan_id, plan_version, state_revision, active_node_id
  decisions[]: DecisionRecord
  state_revision, checksum

DecisionRecord:
  decision_id, revision, status: active | superseded
  statement, rationale_summary, evidence_refs[]
  supersedes_id?, created_at, updated_at
```

原始请求保存在已有受控会话持久层或稳定 artifact 中，引用与 checksum 一起封装。硬约束保留原文和来源 turn，只有用户明确变更目标/约束时才更新；模型摘要不能改写。决策是主 Agent 确认后的记录，保留旧版但仅 active 版本可进入“当前决策”视图。DecisionRecord 不是事实证明；其证据若 stale/unknown，决策投影标 `needs_review`，不能作为已核实代码事实。

Plan 的节点状态、完成条件、依赖和在途结果仍由 Plan reducer/journal 管理。TaskContextState 只保存身份与当前 Plan 引用，不再建立可独立修改的第二份节点状态。若 Plan revision 与 TaskContextState 引用不一致，停止上下文组装并返回 `state_mismatch`，不得取较新的摘要猜测。

## 4. 节点级上下文组装

选定 L2 修复路径在每次模型调用前从当前 Plan revision 生成 `ContextRequest`。至少包含 task/run/plan/node 身份、role、phase、节点目标、完成条件、依赖、目标文件、必需 evidence ID/kind、token budget。Explorer/Patcher/Verifier 沿用当前角色视图；角色过滤不能让 stale 项重新变为有效。

顺序：

1. 从权威状态渲染原始目标的必要部分、全部适用硬约束、当前节点及完成条件；它们占用明确的 `mandatory_budget`，先核算再选择其他项。
2. 校验当前节点所需 Observation/决策的 task/workspace scope、blob checksum 和文件版本；仅 `fresh` 证据进入有效候选集。
3. 用现有 ContextPolicyEngine 选择其他有效 ContextItem，再加入近期对话与历史摘要。不得靠 `hard_pin=True` 代替必需项预算核算。
4. 如果必需状态或必需证据无法装入预算，返回 `context_required_over_budget` 或 `evidence_unavailable`，阻止该次模型调用；可按当前节点重取或使用受限摘要，但不能静默删掉硬约束。原始请求过长时引用保留完整原文，prompt 使用可追溯的任务投影；某项约束无法完整表达时明确失败。
5. 记录 selection manifest：task/run/plan_version/state_revision/node、role/phase、所选与裁剪 item ID、原因、token 估算、预算、freshness 状态、policy version、projection hash。manifest 只存引用与摘要，不存完整源码或模型 prompt。

Plan 节点目标、依赖和完成条件作为一个权威投影，不从 Todo 摘要或 L5 文本拼接。未选中的有效证据保留引用；Agent 可经受控展开接口按需取回。`ContextViewPolicy` 的角色过滤和敏感内容规则继续适用。

## 5. Evidence freshness 与按需重取

本版只承诺**明确依赖文件**的证据。新增紧凑的 `EvidenceDependency` 元数据，关联 Observation ID、仓库相对路径、完整文件内容哈希、产生时 task/workspace、可选 Plan node ID、采集时间。`Observation.source_version` 不得直接当文件哈希：现有调用中它也可能表示工具版本。读取/写入哈希应流式计算，并在既有路径策略内解析，避免把大文件一次读入内存。

消费判定为 `fresh | stale | unknown | missing | denied`：

- `fresh`：作用域、Observation/blob checksum、依赖文件存在及内容版本全部吻合；
- `stale`：已记录的文件版本变化、删除或 Observation 已失效；
- `unknown`：缺少可验证版本、部分/截断检索、无法保证搜索范围未变化，或版本检查失败；
- `missing` / `denied`：引用缺失或不在授权作用域。

只有 `fresh` 可作为当前代码事实、Plan 完成证据或有效决策依据。`unknown` 可作为待核验线索展示，但必须标明不确定性，不能满足完成条件。对非文件证据（例如已经持久化的完整工具收据）按其类型验证收据完整性和 task/attempt 身份，不伪造文件哈希。

文件工具/补丁报告确定的 `changed_files` 后，沿已有 ObservationStore 路径失效能力标记相关证据；依赖它的决策投影和未完成 Plan 节点转入需复核状态。具体 Plan 状态转换由 Plan reducer 执行。对 shell 或外部操作造成但未可靠报告的变更，下一次消费时比较文件版本；执行收据无法确定写集时不得声称“所有证据仍新鲜”。已完成 edit 是历史副作用，不因旧输入证据过期而自动重放。

重取只在当前节点确需使用且证据 stale/unknown 时触发，由主 Agent 通过现有授权工具重新读/测，产生新 Observation，并以 `supersedes`/引用记录旧证据。首期不自动重跑写工具、不在组装上下文时偷偷运行测试或网络命令。对搜索“无结果”如无完整范围版本，重跑搜索后才可重新判断；不实现目录监听器。

`ObservationStore.expand_for_context` 需在现有 checksum/scope/token 限制之前或同一门禁内增加上述 freshness 校验，并返回结构化状态及截断标记。不得将失败展开的摘要冒充原始内容。

## 6. 压缩、外置内容与 Subagent 边界

沿用 L0–L5 压缩与 Observation blob；不复制完整日志到 TaskContextState。压缩摘要只作为历史线索，标注被覆盖 turn/Observation 引用、摘要版本与生成时间。摘要的引用缺失或与权威状态冲突时，保留旧摘要或拒绝新摘要进入有效投影，并发出诊断；原始目标、硬约束、Plan 与 active 决策从结构化来源重新渲染。

较长测试/工具输出继续以 Observation 的受控引用展开，保留现有脱敏、scope、checksum 和 token 限制。部分结果、截断、超时须保留状态，不能被摘要写成完整结论。

AgentResult 首期只做进入上下文前的门禁：任务身份吻合、状态为成功、引用的 Observation 存在且 freshness 可接受、结果未标 partial。失败、取消、超时和未知状态仅可作为进度/诊断，不作成功事实。相同结果可使用现有 Observation dedup；**不声称**完成跨 agent 语义去重或冲突裁决。

## 7. checkpoint 与恢复

在先前 Plan DAG 的 sealed checkpoint/journal 上封装 `TaskContextState` 的 schema/checksum、原始请求引用及 checksum、active 决策引用、当前 Plan 身份、context manifest 引用和所有**仍被当前节点/决策/选中上下文引用**的 Observation ID/checksum/文件版本。活动引用不能因当前 checkpoint 的“最近 100 条 Observation”窗口被截掉；历史审计项可按既有保留策略处理。

恢复顺序：

1. 先由 Plan DAG 恢复器完成在途 attempt、收据及未知副作用判定；它返回安全的当前 Plan revision/节点或 `uncertain`。
2. 校验 task/run/workspace/session、TaskContextState 与原始请求 checksum、Plan 引用、决策链、被引用 Observation 的 scope/blob checksum/文件版本。
3. 缺失或 stale 的代码证据仅使相关决策和节点待复核；无关证据继续可用。身份、schema、权威状态 checksum 或 Plan revision 不一致时拒绝自动继续。
4. 当前节点必需证据不完整时返回 `needs_retrieval`，由主 Agent 先重取；未知写入仍遵循 Plan 的 `uncertain` 规则，不能自动重放。
5. 基于恢复后的权威状态重新组装上下文，写入恢复报告和事件；旧压缩摘要或旧 context manifest 只作为校验对象，不能作为新的事实源。

不新增第二套进程/工具恢复器，不把 checkpoint 当成可恢复的模型隐藏状态。旧 checkpoint 缺少新 schema 时明确以旧模式恢复或拒绝本功能自动接续，不能默默填默认值宣称已校验。

## 8. 事件、验收与交付条件

沿用现有事件链，增加 context_assembled/context_blocked、evidence_freshness_checked、evidence_invalidated、evidence_retrieval_needed、decision_superseded、summary_validation_failed、context_resume_checked。事件含 task/run/plan/node、context revision、Observation/item ID、状态、原因、预算和耗时；默认不含源码、完整 prompt 或完整日志。

| ID | 固定场景 | 必须观察到的结果 |
|---|---|---|
| C1 | 多轮 L5 压缩 | 原始目标、硬约束、当前 Plan 节点及完成条件由权威状态重组；错误摘要不能覆盖它们。 |
| C2 | Explorer/Patcher/Verifier 面对不同节点 | 上下文不同；必需项在预算内完整保留，裁剪原因与引用可查；不足则显式阻断。 |
| C3 | 文件修改、删除、未知写集 | 依赖旧版本的证据 stale/unknown；无关证据仍 fresh；已完成 edit 不自动重放。 |
| C4 | Observation blob 损坏、scope 错误、截断/超预算展开 | 拒绝或标明 partial；摘要不冒充原文。 |
| C5 | 搜索无命中、目录新增文件 | 无完整范围版本时维持 unknown；重新搜索后才更新结论。 |
| C6 | checkpoint 恢复，含活动 Plan attempt | 先完成 Plan 在途恢复，再核验上下文；未知副作用阻断，过期证据需重取。 |
| C7 | 原始请求/Plan/决策/Observation checksum 或身份不一致 | 给出稳定诊断并拒绝自动续用相应权威状态。 |
| C8 | Subagent 部分/失败结果 | 不纳入成功事实，保留可见诊断与引用。 |
| C9 | 一条真实 L2 多阶段修复 | 压缩、修改与恢复后仍能按当前节点完成，manifest/trace/收据可复核。 |

固定 fixture 加至少一条真实 L2 修复路径；记录需求保留、过期证据拒绝次数、重取次数、上下文 token、恢复判定及耗时，只报告实测结果。不做启用/禁用的性能提升宣称。只运行相关测试；全量测试按 `CLAUDE.md` 需用户显式授权。开发开始前保护当前未提交工作，不 reset/stash/覆盖。配套开发计划见 [long-task-context-evidence-mvp](../plans/2026-09-30-long-task-context-evidence-mvp.md)。
