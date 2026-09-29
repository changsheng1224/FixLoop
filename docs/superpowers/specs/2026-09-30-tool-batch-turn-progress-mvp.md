# FixLoop Tool Batch 与 Turn 内实时进度 MVP Spec

日期：2026-09-30。状态：开发规格；功能尚未实现。

## 1. 目标、范围与工期

让一次原生模型响应中的多个**互不依赖且可信只读**工具调用成为独立调用实例，最多两路并发执行；同一工具可用不同 `call_id` 和参数并发。所有调用继续经过工具注册、参数、权限、预算、配额和结果规范化闸口。执行时通过结构化事件即时展示当前 Turn 的阶段、调用状态和完成数量；模型收到的结果仍与原始调用顺序及 `tool_use_id` 配对。

本版交付：调用级身份与隔离、受限只读批次调度、现有执行闸口的并发安全、事件进度投影、固定并发/取消/结果配对案例。单人预计 8–14 个有效工作日，含相关测试和集成返工；约 900–1,500 行实现加测试仅用于规划。

在此前 [Plan DAG MVP](./2026-09-30-plan-dag-inflight-resume-mvp.md) 中，任务级探索最多两路只读并行；本版是**同一次模型响应中的工具调用级并行**，两者共用全局只读并发上限和预算，不能各自开两路而合计四路。Plan 的 journal/recovery 仍是任务恢复权威。本版不依赖 Plan 已实现才可做独立批次，但若对外宣称“完整在途恢复”，必须由 Plan 的真实 attempt 收据支撑。

## 2. 非目标及当前基础

首期不做 `input_from` 结果字段映射、同一模型响应内的多跳工具依赖、任意 Tool DAG 协议扩展、并行写/测试/命令、fail-fast 策略、每工具动态并发配置、复杂 Web UI 或独立的批次恢复器。需要前置结果的后续工具由**下一次模型响应**提出。XML/文本工具调用路径保持原有顺序语义。

当前 `ModelTurnResult.tool_calls` 已含 `call_id`；AgentLoop 的 native 路径按列表顺序逐个调用 `_run_tool_step`。`ToolDAGExecutor` 仅是独立原型，未接该路径；其失败依赖节点放入结果后未从 `pending` 移除，复用前需修复。`ToolExecutor.execute_gated` 从共享 session 的 `_pending_canonical_tool_call` 读取并写 `_last_canonical_tool_call`，执行中临时修改共享 `ToolContext`；AgentLoop 也使用 `_in_flight_tool`、`_in_flight_action`、以 `run:step:tool_name` 构造的幂等键。不能把当前 `_run_tool_step` 直接放进线程池。

现有 `ToolResult` 有 typed status/data/receipt；`Canonical Trace` 有 run 级 seq；CLIProgressCallback 能打印阶段和工具完成，但没有调用级运行中/排队事件。既有 [WSL 沙箱 MVP](./2026-09-30-wsl-command-sandbox-mvp.md) 的命令/测试 profile 为单并发；本版只选文件/文本只读工具，并且每个候选工具都要证明可并发、可受控取消或可安全丢弃迟到结果。

## 3. 批次与调用契约

在 native 模型响应归一化后形成 `ToolCallBatch`，使用响应顺序作为稳定 `ordinal`：

```text
ToolCallBatch:
  batch_id, task_id?, run_id, turn_id, model_response_id?, created_at
  calls[]                                  # 保留模型返回顺序
  status: pending | running | completed | cancelled | uncertain

BatchCall:
  call_id, ordinal, tool_name, arguments, arguments_hash
  source="native", trusted_side_effect="read", idempotency_key
  status: queued | running | succeeded | failed | rejected | cancelled | uncertain
  result_ref?, receipt_ref?, error_code?, started_at?, ended_at?
```

`call_id` 优先沿用 provider 的 tool use ID；空 ID 或同批重复 ID 时整批返回稳定协议错误，任何调用均不执行，避免结果无法配对。`batch_id/turn_id` 在本次响应/Turn 内唯一；幂等键至少包含 run、turn、batch、call 与参数 hash，同一工具名不是调用身份。所有身份由可信运行时生成/核对，模型不能指定副作用类别或执行资源。

进入并行路径的前置校验：批次最多 4 个调用；每个调用名在本 Turn 固定注册表中；副作用类别在可信 allowlist；参数基本结构可解析。空/重复 call_id 等无法配对的结构错误整批拒绝；超过 4 个调用则按现有顺序路径执行并记录容量降级，不丢弃模型调用。各调用的细粒度 schema、权限、审批、配额等仍由 ToolExecutor 逐一判定；一个调用被拒绝不阻止其它独立调用。若批次包含任意写、命令、测试或不能证明并发安全的工具，**整批按现有原始顺序执行**，不让只读与副作用调用交叉执行；仍可发调用级事件。允许只读并行的具体工具名单需在 P0 依据实现审计固定，不能由模型参数扩充。

结果数组按 ordinal 排序，返回给 provider 的每项仍使用原 `call_id` 作为 `tool_use_id`。完成顺序只影响事件时序，不影响工具结果配对。每个 call 的错误或拒绝也形成一项结果，不遗漏 provider 所期待的 tool result。

## 4. 执行、预算与调用隔离

新批次调度器最多持有两个 read permit；同一 Turn 同时只调度一个 batch。若 Plan 任务级探索也并行，共用同一个按 workspace/run 限定的只读 permit 池，不能叠加上限。还要先预留整体工具调用预算与每调用许可，达到额度的调用返回既有 budget rejection，不能线程竞争后超额。对只读路径有最小的调用级 `ToolCallContext`：call/turn/batch 身份、参数 hash、取消 token、可信 ToolContext 快照、预算 reservation、事件 emitter。每个执行实例独立，禁止 worker 在共享 session 单槽位写 `_pending/_last_canonical_tool_call` 或 `_in_flight_action`。

ToolExecutor 保留完整九道闸口，但接口显式接收调用上下文；每个 worker 获得独立 ToolContext（工作区策略与只读配置可共享不可变值）。共享配额、重复检测、resilience 计数、Observation metadata 与事件序号以锁或原子 reservation 管理；线程不直接推进 TaskState、Plan reducer、历史记录或 checkpoint。worker 返回结构化 `ToolResult`/receipt 后，由 AgentLoop 主线程按 call_id/ordinal 汇总，写 Observation、history、预算实际用量和 trace。调用结果为 partial/uncertain 时不得当成功结果复用。

真正安全的只读工具也可能启动可信外部进程（如固定 `rg`）。进入 allowlist 前需核对取消/超时、输出限额及退出确认；无法保证停止时从并行 allowlist 移除。工具执行中工作区文件版本发生变化时，不把旧读取当成当前版本事实，沿用 Observation freshness 规则或标记待复核。多个调用相同工具名但不同参数不得被 ToolExecutor 的重复检测错误合并。

## 5. 取消与 Turn 内进度

当前 Turn 结束或用户取消：先关闭 batch 新调度，未启动的调用各自产生 cancelled 结果；运行中的调用收到其独立 token 并在有界期限内等待。纯只读线程若不能强制终止，可丢弃其迟到结果并阻止它修改共享状态；若调用启动子进程，必须依靠其后端确认退出。无法确认资源清理时标 `uncertain` 并附诊断；不能把 `Future.cancel()`、线程池 `shutdown(wait=False)` 或 CancellationToken 置位当作已清理。取消一批调用不影响其它 run/Turn。

事件经过统一 emitter 分配当前 Turn 内单调 `event_seq`，追加到既有 trace 后投递回调，最少包含 run/turn/batch/call、ordinal、状态、工具名、阶段、耗时、错误码和安全短摘要。事件：`turn_started`、`tool_batch_created`、`tool_call_queued`、`tool_call_started`、`tool_call_completed`、`tool_call_cancel_requested`、`tool_call_cancelled`、`tool_call_uncertain`、`turn_completed`。容量等待以 queued + `waiting_capacity` 原因表达；本版模型 native batch 没有依赖边，因此不展示虚构的 `waiting_dependency`。已有 react_phase 可映射阶段，不复制一套阶段真相。

CLI 首期显示当前阶段、运行中工具与 call 短 ID、排队数、完成/失败数及取消清理状态。进度 reducer 以 `(turn_id,batch_id,call_id)` 和 event_seq 幂等应用；重复/乱序事件不使状态倒退或重复计数。trace 可用于 Turn 结束后回放进度，UI 投影不是执行/恢复真相。事件不含完整源码、完整工具输出、模型内部推理或凭据。

## 6. Checkpoint 和恢复边界

批次采用已有模型 Turn 和工具结果路径；本版不新增可重放的批次 journal。若 PlanSession 已关联该 Turn，Plan attempt journal 与调用收据记录 batch/call ID；恢复时由 Plan 恢复器判定已完成、可安全重做或不确定。本版仅从可信收据与 trace 重建进度投影。若没有 Plan journal，崩溃后的未完成只读批次不能宣称完整在途续跑：先确认旧执行已停，再由新 Turn 重做所需读取；不从旧 UI 事件推断工具成功。副作用工具沿既有串行路径及 Action/Plan 的 uncertain 恢复规则，不因本功能自动重放。

checkpoint 可保存当前 turn/batch ID、已确认 call receipt 引用与最近 event_seq 以便展示；活动引用不能被最近 100 条历史截断。事件缺失或损坏时显示 `progress_replay_incomplete`，不影响 Plan journal 的执行事实判断。恢复不持久化线程或 Future 对象。

## 7. 验收矩阵

| ID | 固定场景 | 必须观察到的行为 |
|---|---|---|
| T1 | 一次响应内两个同名只读工具、不同参数 | 真正并发，两个 call ID 的参数、结果、Observation、收据与事件正确对应。 |
| T2 | 不同只读工具并发、两个以上候选 | 峰值不超过共享上限 2；超额调用可见 queued/waiting_capacity；整体预算不超额。 |
| T3 | 重复/空 call ID、未知工具、无效批次结构 | 批次在任何执行前按协议拒绝；provider 收到完整且可配对的错误结果或明确失败。 |
| T4 | 一个只读调用失败/权限拒绝 | 另一个独立调用按策略完成，返回顺序与模型顺序一致。 |
| T5 | 混合只读与写/命令/测试 | 全批按原顺序走现有闸口；无交叉写入，沙箱单并发策略不被突破。 |
| T6 | Turn 中途取消、排队调用和运行调用 | 未启动调用不执行；运行调用清理/丢弃迟到结果；未知清理显示 uncertain。 |
| T7 | 重复/乱序进度事件 | 同一事件流回放产生同一 UI 投影，不回退或重复计数；不泄漏源码。 |
| T8 | 旧 Turn 崩溃并恢复 | UI 显示已确认与未确认调用；Plan 收据决定执行事实，未确认副作用不自动重放。 |
| T9 | 真实 native 模型 Turn | 模型一次发出多个工具调用，CLI 在运行期间显示进度，结果按 call ID 返回并能继续下一轮。 |

T1/T2/T6 使用同步屏障或可控慢工具证明重叠，不能只以耗时下降推断并发。T9 至少跑一条真实可达 AgentLoop native 路径，不以单独 ToolDAGExecutor 测试冒充产品接入。固定批次可对照串行与并行的实际耗时、并发峰值、预算/结果一致性及事件回放；只报告实测值，不预设提效。完成测试遵循 `CLAUDE.md`：相关测试与受影响 lint/format；全量测试须用户显式授权。配套 [开发计划](../plans/2026-09-30-tool-batch-turn-progress-mvp.md)。
