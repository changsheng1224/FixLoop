# FixLoop Tool Batch 与 Turn 内实时进度 MVP 开发计划

日期：2026-09-30。依据：[MVP Spec](../specs/2026-09-30-tool-batch-turn-progress-mvp.md)。状态：MVP 已实现（2026-10-01）；实现边界与 T1–T9 验证见 [交付说明](../../TOOL_BATCH_TURN_PROGRESS.md)。

## 顺序、工期与工作区保护

按 P0 → P1 → P2 → P3 → P4 → P5 实施，单人估计 8–14 个有效工作日，含相关测试与集成返工。当前工作树已有未提交修改；开始前记录受影响文件状态和哈希，不 reset/stash/覆盖。分支、PR 与全量测试遵循 `CLAUDE.md`，不自动 push/合并。

这是一项 native 模型 Turn 的调用级增强。Plan DAG 的任务级并行和恢复仍由其自身 spec 交付；WSL sandbox 命令/测试单并发保持不变。若 Plan 尚未实现，先使用独立的全局 read permit 池并预留与 Plan 集成接口，不能声称两级并发限制已贯通。

## P0：审计与基线 fixture（1–2 天）

1. 标定 provider `ToolCall.call_id` → AgentLoop native 列表 → `_run_tool_step` → ToolExecutor → ToolResult/Observation/trace 的当前数据流。找出 session 临时槽位、ToolContext 临时字段、配额/重复检测和 `TaskState` 写点。
2. 用失败依赖用例复现 `ToolDAGExecutor` blocked 节点未移出 pending 的循环，修复并运行相关测试；确认它仅是原型，不能直接并发调用共享 AgentLoop。
3. 审计文件/文本工具的副作用、外部进程、取消、版本与输出上限，固定最小并行 allowlist；准备同名不同参数慢工具、预算竞争、混合写批次和真实 native Turn fixture。

门禁：T1/T2/T5 的执行入口与预期行为可复现；allowlist 有实现证据。相关测试：`tests/test_tool_runtime_contracts.py`、`tests/test_agent_loop.py`、`tests/test_tool_executor.py`（以实际文件名为准）。

## P1：Batch 模型与调度边界（2–3 天）

模块建议：`agent_runtime/tool_batch.py`，可复用并修正 `tool_dag.py` 的拓扑/执行思想，但 native MVP 不暴露依赖/结果映射协议。

1. 定义 batch/call 身份、ordinal、可信副作用分类、结果数组及错误码；空/重复 call_id 和结构错误在执行前拒绝。
2. 独立只读调用最多两路并发；混合批次整体按原序执行；调用完成后按 ordinal 归并，始终保留原 call_id 供 `tool_use_id` 返回。
3. 共享 read permit 与预算 reservation 原子获取/释放，超过容量显示 queued；与 Plan 并发上限的共享接口固定。
4. 批次取消关闭新派发，未开始调用产生取消结果；运行调用使用 call token 和有界清理判定。失败调用不阻断独立分支。

新增测试建议：`tests/test_tool_batch.py`。覆盖 T1–T6 的调度纯逻辑及同名调用身份。门禁：原型单测通过，峰值并发与结果顺序可由同步屏障证明。

## P2：ToolExecutor/AgentLoop 调用隔离（2–3 天）

模块：`agent_runtime/tool_executor.py`、`agent_loop.py`、ToolContext/ToolResult/预算与 Observation 适配。

1. 增加显式 ToolCallContext；每个 worker 独立 ToolContext/cancel token。移除并发路径对 `_pending/_last_canonical_tool_call`、`_in_flight_action` 单槽位的依赖，幂等键含 turn/batch/call/args hash。
2. 所有 worker 仍调用 ToolExecutor 的完整九道闸口；共享配额、重复窗口和 resilience 状态加锁或用预留收据。禁止 worker 修改 TaskState、history、Plan 或 checkpoint。
3. 主线程按 call_id/ordinal 收集 ToolResult，保存 Observation、工具收据和模型结果；旧顺序路径维持原行为。并发结果与文件版本不一致时标待复核。
4. 真正取消/超时的可达工具要返回受控结果；无法确认退出的外部进程不进入并行 allowlist。

新增测试建议：`tests/test_tool_executor_call_isolation.py`、`tests/test_native_tool_batch.py`，补相关 ToolExecutor/AgentLoop 测试。覆盖 T1/T4/T5/T6/T9 的配对与权限。门禁：两路同名调用不串参数、收据、Observation、预算或取消状态。

## P3：Turn 事件与 CLI 进度（1–2 天）

模块：`agent_runtime/canonical_trace.py`、`callbacks.py`、CLI 进度 adapter、批次事件 emitter。

1. 为 Turn/Batch/Call 状态变更分配单调 event_seq，先追加 trace 再投递实时 callback；复用 react_phase 表示 Agent 阶段。
2. 实现按 call ID 的幂等投影：运行中、等待容量、完成、失败、取消/不确定；按 event_seq 处理重复或乱序。
3. CLI 输出简短进度，不输出源码、完整命令或模型内部推理；Turn 结束后用相同事件流重建展示。

新增测试建议：`tests/test_turn_progress.py`。覆盖 T7/T9。门禁：慢工具运行期间能看到 started/queued，事件回放结果与现场一致。

## P4：checkpoint 关联与取消恢复边界（1–2 天）

模块：`agent_runtime/checkpoint.py`、Plan attempt/receipt adapter、批次进度重建。

1. checkpoint 记录当前 turn/batch ID、已确认 call receipt 引用与 event_seq；活动引用不因最近 100 条截断而丢失。
2. 有 PlanSession 时把 call ID 接到 journal/receipt，由 Plan 恢复器判执行事实；无 Plan 时崩溃中的只读批次不自动承诺续跑，先确认旧执行结束再重取。
3. 旧 trace 缺失或损坏只使 UI 投影报告 `progress_replay_incomplete`；不从 UI 状态推断副作用成功。副作用工具继续走既有串行/uncertain 恢复规则。

新增测试建议：`tests/test_tool_batch_resume_projection.py`，补 `tests/test_checkpoint_resume.py`。覆盖 T8。门禁：恢复后展示与可信收据一致；未知副作用不自动重放。

## P5：真实路径验证与交付（1–2 天）

1. 运行 T1–T9，包含一次真实 native AgentLoop 工具批次、同名不同参数并发、混合写、取消、崩溃后进度回放；保留 call IDs、工具收据、trace、并发峰值和结果顺序。
2. 对固定只读批次记录串行/并行实际耗时、预算与配对一致性，描述工具耗时和环境；不预设提升幅度。
3. 跑相关测试及受影响 lint/format，更新开发说明与面试案例。全量测试仅在用户显式授权时运行。

完成定义：T1–T9 可复核，生产 native 路径可演示单次响应中同名只读调用并发、调用状态实时可见、ToolExecutor 全闸口未绕过、结果配对正确，且不突破 Plan/沙箱并发边界。若前置 Plan/沙箱未落地，应明确报告对应集成项未完成，不借单元 mock 宣称端到端恢复或清理。

## 实施记录（2026-10-01）

P0 审计确认 blocked 节点循环已由此前代码修复，保留回归测试。P1–P5 已接入 native AgentLoop、显式调用隔离、两路共享 permit、原子预算预留、Turn 事件和 CLI 回放。Plan operation 关联 provider call/batch/turn ID，真实进程退出用例验证恢复权威。固定批次实测、相关测试和 lint 见交付说明。未自动 push、PR 或合并，未运行全量测试；独立批次不提供新的执行 journal。
