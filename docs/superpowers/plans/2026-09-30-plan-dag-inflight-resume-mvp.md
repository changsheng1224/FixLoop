# FixLoop Plan DAG 与完整在途任务恢复 MVP 开发计划

日期：2026-09-30。依据：[MVP Spec](../specs/2026-09-30-plan-dag-inflight-resume-mvp.md)。状态：未实现。

## 执行与范围

顺序 P0 → P1 → P2 → P3 → P4 → P5。预计单人 18–28 个有效工作日，包含相关测试、故障注入和集成返工。每阶段完成门禁后再扩大改动范围。

当前工作区已有未提交修改。开始时记录相关源码和 checkpoint 文件哈希，不 reset/stash/覆盖；如果需要隔离 checkout，先确认它能包含当前已有工作。Git/PR 依 CLAUDE.md，不自动 push/合并。没有对照实验。

## P0：基线与失败分支核对（1–2 天）

目标：固定一条 L2 修复入口及 Plan/ToolDAG 当前行为，避免做完通用模块后无法接入产品路径。

任务：

1. 标定当前 Todo 创建、推进、skip_plan、Orchestrator→Patcher→Verifier、ToolExecutor/action ledger、Observation、checkpoint 与 step-resume 的调用链；标出 Plan 发生在工具探索前的现有时序。
2. 用最小失败依赖用例复现 ToolDAGExecutor blocked 节点留在 pending 的问题，记录根因与退出条件；再修正并跑原有相关测试。
3. 选择真实 L2 修复路径接入点及计划责任边界：先由同一 Patcher Agent 做受限只读预规划探索；Plan 控制依赖/完成，Orchestrator 保留重试/验证/终态并向 verify 节点回灌实际结果。
4. 定义最多 8 节点、2 只读并行、2 次重规划的固定配置；审核工具可信副作用类别及原有共享 ToolContext 风险。
5. 先设计故障注入 fixture 和持久化切点；记录文件系统原子写与落盘能力限制。

相关测试：`tests/test_tool_runtime_contracts.py`、`tests/test_agent_loop.py`、`tests/test_checkpoint_resume.py`。验收：失败依赖不会循环；执行边界图与崩溃切点表可用于 P1–P4 实现。

预计实现 30–70 行，测试 40–80 行；调查和记录另计。

## P1：Plan 模型、reducer 与校验（2–3 天）

模块：`agent_runtime/plan_runtime/models.py`、`validate.py`、`reducer.py`、相关 schema/错误码。

任务：

1. 实现 Plan、PlanNode、NodeAttempt、typed completion 和序列化/反序列化。
2. 分离 plan_version/state_revision；状态只能由 reducer 产生新快照，不允许 Agent/调度器直接 mutate Plan。
3. 校验无环、唯一 ID、依赖、身份、工具注册表能力、预算、条件类型和稳定节点 ID。
4. 明确 blocked、stale、uncertain 转换、历史成功节点语义和旧 attempt 迟到结果处理。
5. 用版本化 schema/checksum 保存历史 Plan revision；非法候选不部分提交。

新增测试 `tests/test_plan_models.py`、`tests/test_plan_reducer.py`，覆盖 D1/D2/D5/D6 的纯状态部分及所有合法/非法转换。完成门禁：同一事件序列重放得到相同计划状态，工具 success 不会自动使节点 succeed。

预计实现 200–300 行，测试 140–220 行。

## P2：探索生成、证据与受限调度（3–4 天）

模块：`plan_runtime/evidence.py`、`scheduler.py`，AgentLoop 与 ToolExecutor 最小接入，L2 任务入口。

任务：

1. 重排选定 L2 路径的时序：主 Patcher Agent 先以最多 4 次只读工具调用探索并写入 Observation，再生成结构化候选 Plan；未经证实的位置/符号作为假设；生成失败可回到通过相同校验的顺序小计划。
2. typed completion 引用现有 Observation、工具收据和验证记录；文件版本未知不自动确认成功。
3. 固定只读工具白名单和独立 per-node ToolContext/cancel token/预算子额；主线程归并证据，最大并行度 2。
4. edit/analyze/verify 只由主 Agent 串行执行；写前依赖再验证，写后记录变更与相关证据失效。不能重用当前共享 ToolContext 的可变字段做并发执行。
5. 一条真实 L2 修复入口使用跨 Patcher ask/Verifier 调用的 PlanSession；Orchestrator 回灌最终验证收据、映射重试/终态，不为 verify 节点重复执行测试。旧普通 Todo 可保留在非 Plan 模式，但 Plan 模式不能同时推进两套状态。
6. 计划事件和最小用户进度进入现有 Canonical Trace/回调。

新增测试 `tests/test_plan_scheduler.py`、`tests/test_plan_evidence.py`、`tests/test_plan_l2_binding.py`；相关现有测试 `tests/test_tool_executor.py`、`tests/test_context_runtime_governance.py`（存在时）及 `tests/test_l2_binding.py`。验收：D3–D7/D15 正常路径通过，真实 Agent 修复路径中 plan_created 与实际编辑/验证有同一 task/run 引用。

预计实现 300–450 行，测试 180–260 行。

## P3：持久 attempt journal 与 checkpoint 封装（3–5 天）

模块：`plan_runtime/journal.py`、`session_store.py`、`task_state.py`、`checkpoint.py`、`session_contract.py`、AgentLoop 工具派发边界。

任务：

1. 建立 prepared→dispatched→result_recorded→reconciled 的 journal 记录与原子更新，派发前 durable 意图，工具返回后先 durable 结果；预规划只读探索调用也记入同一 run 的 journal。
2. journal 与既有 ToolExecutor call_id、action ledger、receipt、Observation ID 一一关联；不复制原始源码/完整输出到 Plan。
3. checkpoint envelope seal Plan checksum、当前 revision 与活动 attempt manifest；恢复时能检查 Plan/journal/checkpoint 反向关联。
4. 旧 checkpoint schema 的兼容方式由真实契约决定：无法安全迁移旧 `plan_todos` 时，明确只支持旧任务按旧模式恢复，不把它伪装为 DAG Plan。
5. 活动 attempt/收据不可被历史截断窗口丢弃；垃圾回收只处理无活动引用的老版本。
6. 故障注入在派发前、派发后、结果持久化后、reducer 后及 checkpoint 前断开进程。

新增测试 `tests/test_plan_journal.py`、`tests/test_plan_checkpoint.py`、`tests/test_plan_crash_windows.py`；相关现有 `tests/test_checkpoint_resume.py`、`tests/test_strong_step_resume.py`、`tests/test_session_bak.py`。验收：D9/D10 中每个切点可重建准确的上一次可信状态，不误认 session 内未持久缓存。

预计实现 240–360 行，测试 180–280 行。

## P4：完整在途恢复与静止点重规划（4–6 天）

模块：`plan_runtime/recovery.py`、`evidence.py`、`scheduler.py`、`checkpoint.py`、执行器取消/查询接口、L2 映射。

任务：

1. 实现恢复算法：身份/完整性 → reconcile 旧执行 → 收据/Observation/工作区版本 → 判节点终态 → 安全续跑。
2. 只读旧调用（包括预规划探索）必须确认停止后才能新 attempt；写调用有可信成功收据与后态时接纳，无证据时 uncertain 且阻止潜在冲突操作。
3. 验证节点若有完整可信结果则接纳，否则确认旧进程退出后重新验证，不把部分输出当 passed。
4. 重启期间防旧迟到结果污染当前计划；Plan 版本与 attempt ID 必须匹配。共享工作区仍有不明执行时保持隔离/阻塞。
5. 静止点局部重规划，保留旧版与触发证据；只沿用新鲜 explore/analyze 和经过后态核验的 edit 历史，不自动重放写入。
6. 无法判定时输出结构化 uncertain 报告给主 Agent；Agent 可以先检查 diff/测试再提交新计划，但恢复器不得自动重试未知副作用。

新增测试 `tests/test_plan_recovery.py`、`tests/test_plan_replan.py`、`tests/test_plan_process_crash.py`；相关现有 `tests/test_resume_repair.py`、`tests/test_strong_step_resume.py`、`tests/test_tool_timeout.py`。验收：D8–D14，包含真实进程级中断、部分写入、重复启动保护、损坏收据、workspace/schema 不匹配。仅单元 mock 不算完成。

预计实现 280–420 行，测试 240–360 行。

## P5：端到端验收和交付（2–3 天）

任务：

1. 运行 D1–D15 固定任务集，包含一次普通真实 L2 修复与一次中途终止后恢复；保留 journal、checkpoint、trace、diff 和验证记录。
2. 逐项核对实际工具调用次数，确保已确认成功的补丁没有被重放，未知副作用没有被静默继续。
3. 检查 node/attempt/plan 关联、用户进度、事件内容、预算与阶段权限；更新开发文档和面试讲解示例。
4. 运行改动相关测试与 lint/format；全量测试仅在用户显式授权时运行。
5. 报告通过、失败、未覆盖和能力限制，不给未经对照的速度或成功率提升结论。

建议证据目录：

```text
eval_results/plan_dag_resume_mvp/<run-id>/
  environment_manifest.json
  cases.jsonl
  plans/
  journals/
  checkpoints/
  traces/
  diffs/
  report.md
```

每个 case 记录 fixture/源码版本、task/run/plan/attempt、注入切点、恢复判定、工具实际调用次数、旧进程清理状态和工作区后态。成果是恢复契约证据，不是性能对照。

预计 fixture/验收代码 100–180 行，文档另计。

## 完成门禁与范围控制

- P0 确认 ToolDAG 失败分支和 L2 `skip_plan` 入口，不让未接入修复流程的 DAG 冒充已交付。
- Plan 节点条件、依赖、状态经单一 reducer，可信副作用分类不能被 Planner 降级。
- 两路只读并行不共享可变执行上下文；修改、验证串行。
- 持久意图、结果与 checkpoint 能覆盖所有列出的在途中断点。
- 旧执行未确定停止时不派发冲突操作；写入不确定不自动重放。
- 有可信收据的结果能被接纳并安全续跑，过期证据只影响相关派生节点。
- D1–D15 有确定性及必要的进程级证据，至少一次真实 L2 修复正常及中断恢复。
- 不执行当前 Todo/DAG 对照实验，不宣称修复率或耗时提升。
- 相关测试与 lint/format通过；全量测试仅获授权后执行。

P2 不扩展成多个 LLM Worker。P4 不加入运行时任意图编辑或自动重放未知写入。无法解决旧执行清理/可验证收据时，保持 uncertain 并报告限制，不通过猜测补齐“完整恢复”。
