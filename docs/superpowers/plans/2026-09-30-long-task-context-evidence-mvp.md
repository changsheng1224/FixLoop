# FixLoop 长任务上下文与证据管理 MVP 开发计划

日期：2026-09-30；状态对账：2026-10-02。依据：[MVP Spec](../specs/2026-09-30-long-task-context-evidence-mvp.md)。状态：完整原范围部分实现；本轮确认的统一状态投影、必需预算门禁和恢复投影已完成，见 [验收记录](../../LONG_TASK_CONTEXT_ACCEPTANCE_2026-10-01.md)。决策版本、超长目标语义投影和其他入口迁移未实施。本文件保留原 P0–P5 拆解，**当前范围以 [2026-10-01 计划](2026-10-01-agent-design-reference-improvements.md)的已确认 MVP 为准**，不得重复建设已有状态模块或将原阶段整体勾完。

## 执行前提与估算

初始完整范围估算为单人 10–15 个有效工作日，不能当作当前剩余工期。本轮讨论收敛为约 2–4 日的差量 MVP，已复用 PlanSession、LongTaskState、预算和 checkpoint；不包含下述完整决策版本目标。普通 L1 Todo 不能替代此接口；下一领域继续单独讨论，不自动开展内核拆分或 Hermes 专项补充。

当前工作树有未提交修改。开发前记录受影响文件的状态与哈希；不 reset/stash/覆盖。分支、远端 PR 和全量测试遵循 `CLAUDE.md`。本计划不做开启/关闭的效果对照实验。

## P0：接口与失败场景基线（1–2 天）

1. 标定 L2 修复入口到 PlanSession、ContextManager、ObservationStore、L5、checkpoint 的实际数据流，确认 Plan 的 task/run/workspace、node_id、attempt、revision 和恢复报告接口。
2. 固定一个多阶段修复 fixture：先探索，经历压缩，修改已读文件，保存 checkpoint，再恢复验证。再准备 blob 损坏、scope 错误、预算不足和部分 Subagent 结果 fixture。
3. 明确原始请求稳定存储位置、已有敏感内容与路径策略、文件 hash 的流式读取接口；对 `source_version`、`changed_files`、最近 100 条 Observation manifest 的现状写出接入清单。

门禁：可以画出唯一权威状态与持久化顺序，测试能先复现至少“压缩后权威来源不明确”或“文件变更后旧证据可展开”之一。建议相关测试：`tests/test_context_manager.py`、`tests/test_context_runtime_governance.py`、`tests/test_checkpoint_resume.py` 和 Plan DAG 模块测试（以实际文件名为准）。

## P1：权威状态与决策版本（2–3 天）

模块：扩展已有 `agent_runtime/plan_runtime/long_task.py`、`session.py` 与其 journal；最小接入 L2 任务入口，不新建平行 TaskContextState。字段为目标契约，需按实际差量实施。

1. 在现有 LongTaskState/schema/checksum/序列化基础上补 DecisionRecord 和来源。原始用户请求和硬约束从受信任入口写入，保存稳定引用与 checksum。
2. active/superseded 决策用追加 revision 表达；只有主 Agent 可确认新决策，Subagent/摘要不能直接修改权威状态。
3. Plan 状态由 Plan reducer 维护；本模块只绑定当前 plan_id/version/state_revision/node_id，发现引用不一致时返回 `state_mismatch`。
4. 将现有状态投影改为只读且必要信息优先，独立于压缩历史；freshness 状态更新由显式 owner 操作落盘。

新增测试建议：`tests/test_task_context_state.py`。覆盖目标/约束不被摘要改写、决策 supersede、Plan revision 不一致、schema/checksum 损坏。门禁：相同权威状态得到确定性投影，无第二份可变 Plan 节点状态。

## P2：节点级上下文与预算门禁（2–3 天）

模块：`agent_runtime/context_runtime.py`、`context_manager.py`、`context_projection.py`，可新增 `context_governance/assembly.py`。

1. 每次选定 L2 模型调用前，以当前 Plan 节点构造 ContextRequest；包含完成条件、依赖、目标文件、必需证据与预算。
2. 分配 mandatory_budget 并先渲染目标、约束、当前节点；若超预算或必需引用缺失，返回结构化阻断，不调用模型。`hard_pin` 继续用于优先级，不能充当保证。
3. 对有效证据沿用 ContextPolicyEngine；角色视图仍过滤候选，stale/unknown 不作为已确认事实。将已选、裁剪、原因、Plan revision、freshness 与预算写入 context manifest。
4. 历史摘要仅进可选候选。summary 与权威状态冲突时按权威状态投影，并记录诊断；不修改原始请求。

新增测试建议：`tests/test_plan_context_assembly.py`，补 `tests/test_context_manager.py`。覆盖 C1/C2 的预算、角色与错误摘要。门禁：必需项不可静默被裁掉，manifest 能解释每项选择。

## P3：文件证据 freshness 与受控展开（2–3 天）

模块：`agent_runtime/context_runtime.py`、`agent_runtime/agent_loop.py`、工具收据适配；可新增 `context_governance/freshness.py`。

1. 为明确依赖文件的 Observation 保存相对路径与独立完整文件 hash，不复用含义不确定的 `source_version`；哈希流式计算，沿现有安全路径解析。
2. 实现 `fresh/stale/unknown/missing/denied` 检查。Observation 的 task/workspace、blob checksum、内容版本都必须成立；无法核验返回 unknown。
3. 工具写后对确定 changed_files 调用既有失效流程；消费/展开前再次校验，覆盖外部变更和未报告写集。相关决策投影与 Plan 证据由现有 Plan reducer 标待复核。
4. 在 `expand_for_context` 入口统一校验、截断与原因。无完整范围版本的负向搜索保持 unknown，按需重跑既有搜索工具，不建目录监听器。

新增测试建议：`tests/test_context_evidence_freshness.py`，补 Observation/工具结果测试。覆盖 C3–C5。门禁：旧证据无法作为 fresh 进入 prompt；无关证据可复用；写节点不因失效被自动重放。

## P4：checkpoint 封装与恢复衔接（2–3 天）

模块：`agent_runtime/checkpoint.py`、`session_contract.py`、Plan 恢复器适配，必要时调整 context manifest 序列化。

1. 在现有 sealed envelope 内关联 LongTaskState checksum、原始请求引用、active 决策链、Plan 身份、当前节点所需 Observation 清单及版本；复用已有 Plan 归档，活动引用不能被“最近 100 条”截断。
2. 恢复先等待 Plan DAG 的 attempt reconciliation；写入 uncertain 时直接阻断上下文续跑。
3. 校验权威状态、Plan revision、Observation scope/checksum/文件版本。权威身份/完整性不符拒绝自动恢复；局部证据过期只标相关节点待重取。
4. 恢复后重新组装当前节点上下文并记录报告；旧摘要/旧 manifest 不当成事实源。保持旧 schema 明确诊断，不伪装已核验。

新增测试建议：`tests/test_context_checkpoint_recovery.py`，补 `tests/test_checkpoint_resume.py` 和 Plan 恢复测试。覆盖 C6/C7，至少一次在途 Plan attempt 故障注入。门禁：不会重放未知写入，恢复后仍按当前节点和有效证据继续。

## P5：真实路径验收与交付（1–2 天）

1. 在一条真实 L2 修复流程运行 C1–C9，保留任务输入、Plan revision、context manifest、Observation ID、版本、checkpoint、恢复报告及工具收据；默认脱敏。
2. 验证 Subagent 失败/partial 结果不进入成功事实；不加入语义去重/冲突系统。
3. 汇总实测 token、过期证据拒绝数、重取次数、恢复状态和耗时，不预设收益；记录未覆盖的负向搜索与跨任务场景。
4. 运行相关测试与受影响文件 lint/format。全量测试仅在用户明确授权时运行；按仓库 Git/PR 规范交付，不自动 push 或合并。

完成定义：spec 的 C1–C9 均有可复核证据，至少一条真实修复路径完成压缩→修改→中途恢复→后续验证，且权威状态、Plan 状态与代码证据一致。若 Plan DAG 前提未满足，只报告已完成的独立部分，不宣称整体完成。
