# FixLoop Subagent 并行探索与证据汇总 MVP 开发计划

日期：2026-09-30。依据：[MVP Spec](../specs/2026-09-30-subagent-readonly-exploration-mvp.md)。状态：2026-10-01 已实现并完成受控模型的真实 runtime 验证；详见[验收记录](2026-10-01-subagent-readonly-acceptance.md)。未进行在线模型评测。

## 前提、工作量与工作区保护

按 P0 → P1 → P2 → P3 → P4 → P5 实施；前述 Plan DAG、上下文证据 freshness、Turn 进度的生产契约已接入同一 L2 修复路径时，预计单人 10–16 个有效工作日。文件/文本工具即可完成首期，LSP/Code Graph 是可选增强。前置规格尚未落地时，只能交付独立协作组件和 fixture，不能以旧 Todo/旧 Observation 机制冒充完整验收。

当前工作树有未提交修改。开发前记录相关文件哈希与状态，不 reset/stash/覆盖。分支、PR、相关测试及全量测试授权遵循 `CLAUDE.md`；不自动 push 或合并。

## P0：路径审计与失败 fixture（1–2 天）

1. 标定真实 L2 主 Agent 工具注册、AgentFactory/ToolGateway、CollaborationStore/TaskScheduler、ObservationStore、PlanSession、Turn trace 的实际入口和 ID；固定 `implementation_location`、`related_tests` 两类输入。
2. 审计所有 explorer 可达工具的副作用、路径策略、子进程取消和输出上限；固定只读 allowlist，不复用 patcher/verifier 的全量工具注册表。
3. 用最小 fixture 复现批量提交非法任务可能留下部分记录，以及 `TaskScheduler.run_once` 同步执行的现状；记录跨线程共享 dag 与内存 BudgetLedger 的风险。准备两个慢探索、越权写、版本变化、取消中 worker 丢失样例。

门禁：可画出一条真实 L2 委派路径，所有潜在写入口已识别，A1/A2/A3 的 fixture 可复现。相关测试：`tests/test_collaboration_runtime.py`、`tests/test_context_runtime_governance.py`、Plan/Turn 测试（按实际文件名调整）。

## P1：委派接口、批量校验与句柄（2–3 天）

模块建议：`src/collaboration/exploration_contracts.py`、`delegation.py`，扩展 `store.py` 的批量事务；L2 主 Agent 工具注册最小接入。L1 通用 Agent runtime 不 import `src`。

1. 定义 ExplorerTask/ExplorationResult schema、稳定句柄、结果引用、attempt/lease generation；可信 runtime 填入 scope/Plan/预算信息，模型只提供 kind/question/可选受限 scope 与证据 ID。
2. 实现整批先校验再事务写入，拒绝重复 kind、数量超限、越权 scope、失效输入证据与预算不足，不留下半批任务。
3. 实现 `delegate_exploration` 和 `collect_exploration`；collect 限同父 task/run、 bounded wait、重复回收幂等，超时返回当前状态。

新增测试建议：`tests/test_exploration_delegation.py`、`tests/test_exploration_store.py`。覆盖 A3/A4 的提交/句柄边界。门禁：主 Agent 工具可得到稳定句柄，但尚不声称真实并发。

## P2：专用只读 Agent 与真实并行调度（2–3 天）

模块：`src/agents/factory.py` 或专用 explorer factory、`src/collaboration/exploration_runtime.py`、ToolGateway/ToolContext、预算与 worker 生命周期。

1. 为每个子任务建立独立 Agent/session/ToolContext/cancel token，输入只含委派目标、当前 Plan 节点投影和已核验证据摘要/引用；固定最多 3 次模型调用、4 次只读工具调用。
2. 两路 worker 使用 CollaborationStore claim/CAS 和 per-task attempt；不并发共享未加锁的 `TaskScheduler.dag`。全局 read/model permit 与 Plan/Tool Batch 对齐，预算预留/结算以 task/attempt 为键。
3. 注册表与 Gateway/ToolExecutor 双层禁止写、补丁、shell、测试和旁路执行；主 Agent 修改前等待/取消探索并检查旧版本。
4. 完成、失败、timeout、启动失败均释放并发资源；usage 未知按预留上限结算，不能重复花费。

新增测试建议：`tests/test_explorer_agent_isolation.py`、`tests/test_exploration_workers.py`，补相关 ToolGateway 测试。覆盖 A1/A2/A4。门禁：同一真实 run 中两个 Agent 循环重叠、工作区无子任务写入、预算上限成立。

## P3：结构化证据与主 Agent 汇总（2–3 天）

模块：`src/collaboration/exploration_results.py`、Observation adapter、主 Agent 收集/Plan 更新入口。

1. 子任务输出固定 finding/unknown/coverage schema；将原始工具输出留在 ObservationStore，只存引用与短摘要。无结果与截断不冒充全仓结论；测试发现不得写成测试执行/覆盖。
2. collect 时重验 workspace/task/Plan、Observation checksum 与文件 hash；过期项标 stale，partial 不进入已确认事实。
3. 仅确定性完全重复的 finding 合并来源；相同目标键但不一致陈述标 needs_review，主 Agent复核后才经 Plan reducer接纳。Blackboard 仅存受限候选 proposal，不用 highest_confidence 选赢家。

新增测试建议：`tests/test_exploration_evidence.py`、`tests/test_exploration_merge.py`。覆盖 A5/A6/A10 的状态逻辑。门禁：有效、过期、部分和冲突发现明确区分；Subagent 无法直接推进 Plan 或修改主决策。

## P4：取消、恢复与 Turn 事件（2–3 天）

模块：`src/collaboration/exploration_runtime.py`、store lease/结果收据、checkpoint 适配、Turn 事件 emitter。

1. 父取消停止新 claim 并传播到子 Agent/只读工具；有界等待，清理未确认保留 worker_lost/诊断，旧 attempt 迟到结果被 lease generation 拒绝。
2. checkpoint 封装 task IDs、attempt/lease、预算 reservation、结果/Observation refs、Plan revision 与事件水位；恢复先确认旧 worker 不再执行，再重派只读新 attempt 或复用新鲜完成结果。
3. 发提交/排队/开始/完成/partial/失败/取消/证据过期/待复核事件；复用 Turn 进度 reducer 幂等回放，不将 UI 事件当恢复真相。

新增测试建议：`tests/test_exploration_cancel_resume.py`、`tests/test_exploration_progress.py`，补 checkpoint/Plan 恢复测试。覆盖 A7–A9。门禁：取消后无新启动，旧 worker 不覆盖新结果，预算与 UI 状态恢复一致。

## P5：真实修复闭环与实测记录（1–2 天）

1. 运行 A1–A10，至少一条真实 L2 修复：主 Agent 委派定位与测试发现、收集有来源结果、确认/拒绝发现、更新 Plan、独自修改与验证；保留模型/tool/Observation/Plan/trace/工作区后态记录。
2. 用固定任务记录串行探索与两路 Subagent 的定位覆盖、额外 token、调用数、并发峰值、重复工作和耗时，只报告实测值。
3. 运行相关测试及受影响 lint/format，写明未覆盖的任意 DAG、递归委派、语义冲突和跨任务缓存；全量测试仅在用户显式授权时运行。

完成定义：两路真实只读子 Agent 可并行，稳定句柄能查询/等待/幂等回收，越权工具被拒绝，返回的发现有可校验证据且过期不被主 Agent 采纳，取消/恢复不形成双 worker，真实 L2 修改仍由主 Agent 串行执行。任一前置契约未落地时，报告对应未完成项，不以 mock 代替端到端能力。
