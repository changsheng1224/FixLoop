# FixLoop 并发恢复互斥与取消清理闭环 MVP Spec

日期：2026-09-30。状态：开发规格；功能尚未实现。

## 1. 目标、范围与前提

在已有 Plan DAG 完整在途恢复和 WSL 沙箱进程管理之上，保证同一修复 run 只有一个执行者能够恢复并继续派发；取消时停止新派发，向已登记的活动资源传播取消，核对退出和副作用结果后才提交任务终态。重点处理并发 resume、旧执行者迟到提交、取消中的进程残留与未知写入。

本版交付：

1. run 级恢复所有权、单调 generation 与提交/派发门禁；
2. 关联 Plan node/attempt、Subagent task、沙箱 call 的最小活动资源登记；
3. `cancel_requested → cancelling → cancelled | recovery_required` 的持久转换与清理收据；
4. 固定故障注入与一条真实 L2 修复路径的取消/恢复验证。

预计单人 **6–10 个有效工作日**，含相关测试和集成返工；约 600–1,100 行实现加测试用于规划，不作为验收指标。该估计以 [Plan DAG 完整在途恢复 MVP](./2026-09-30-plan-dag-inflight-resume-mvp.md) 已提供 PlanSession/reducer/durable attempt journal，以 [WSL 命令与测试沙箱 MVP](./2026-09-30-wsl-command-sandbox-mvp.md) 已提供 supervisor 的 cancel/reconcile/cleanup 收据为前提。两者目前仍是规格文档；前提未落地前只能实现隔离的模型与测试，不能宣称端到端交付。

## 2. 现有基础、责任边界和非目标

`TaskState` 追踪单次 ask 的运行与停止，`CancellationToken` 是协作式信号；AgentLoop/Orchestrator 已向部分工具和验证路径传播取消。`CheckpointEnvelope`、Action Ledger 与 step resume 有基础，但当前 Action Ledger 在会话中保留最近 100 条，不承担崩溃窗口的 durable 真相。`src/collaboration/store.py` 有单个 AgentTask 的租约和版本 CAS；它不是整个 L2 repair run 的 owner/fencing。`ThreadPoolExecutor.shutdown(wait=False)` 也不证明底层进程已退出。

职责分配：

| 组件 | 唯一职责 |
|---|---|
| PlanSession/reducer/attempt journal | 节点状态、工具结果、写入是否生效及完整在途恢复；本版不得复制第二份 Action 状态机。 |
| WSL supervisor/registry | 命令、测试及后台子进程的实际取消、终止、等待、残留核查和执行收据。 |
| 本版 RunCoordinator | 同一 run 的执行所有权、generation、资源登记、取消协调、终态门禁与清理报告。 |
| L2 Orchestrator | 将真实修复 run 的 Plan、Subagent 和验证调用接入 RunCoordinator；保留既有修复/验证决策。 |
| Canonical Trace | 审计和诊断投影；不能充当唯一恢复来源。 |

首期限定同一可信 state_root 下的一条 L2 修复路径和本机/WSL 控制器。资源类型仅覆盖 Plan node attempt、`AgentTask` 和 WSL sandbox call；不支持任意外部服务、远端机器、持久后台任务、通用工作流引擎、全局多租户调度或任意进程 PID 清理。无受控 cancel/reconcile 适配器的可执行资源不能在该 profile 登记为“可安全取消”，应拒绝启动或明确 `recovery_required`。不新增第二套事件溯源平台，也不重复前述 Plan spec 的崩溃窗口和上下文证据失效实现。

## 3. Run owner 与 generation 契约

控制面在 PlanStore 同一受信任 state_root 保存 `RunOwner`，建议 schema：

```text
RunOwner(schema_version="1"):
  task_id, run_id, workspace_id
  generation: int                 # 单调递增，不复用
  owner_id: str                   # 本次控制器实例随机 ID
  phase: reconciling | active | cancelling | released
  lease_expires_at, heartbeat_at
  plan_version, state_revision, journal_sequence
  checksum
```

通过跨进程互斥和原子 compare-and-swap 获取/更新 owner，成功提交一次接管才递增 generation。重复请求在同一 owner 下返回既有 generation/状态；其他 owner 在租约有效时返回 `resume_owner_conflict`。可参考 `CollaborationStore` 的单任务 CAS，但不把其 task lease 当作 run lease。

**租约过期只允许尝试接管，不允许立即派发。** 新 owner 首先持有 run 执行互斥，进入 `reconciling`；确认旧控制器无法继续派发/提交、旧 Plan attempt 与 sandbox call 已停止或隔离，核对 checkpoint/journal/收据/工作区，再转换 `active`。仍有未知进程或副作用时保持 `recovery_required`，不进入 active。旧 owner 在任一提交、Plan reducer 转换和工具派发前须验证 owner_id + generation；不匹配则拒绝。迟到结果只可进入隔离诊断，不可推进当前 Plan。

为了封住“检查 generation 后、真正启动工具前”的竞态，派发必须经 RunCoordinator 在持有互斥且仍为 active 时登记 attempt/call，随后使用同一 generation 的受控执行入口；新 owner 只有旧互斥释放、活动资源 reconcile 完成后才可开放派发。WSL supervisor 接收可信控制器传入的 run/generation/call 身份并在启动前核对；如果已有沙箱接口不支持此门禁，P0 必须先补足。租约/数据库 fencing 无法撤销已经运行的 OS 进程，必须依赖 supervisor 清理确认与工作区执行锁。文件写工具同样必须经过现有 ToolExecutor/Plan 的串行写门禁；不能存在不受控制的宿主机写路径。

owner heartbeat 丢失时停止本 owner 后续派发并进入核查；不能让本进程静默继续写入。进程崩溃后旧互斥释放不证明旧沙箱进程已死。两个不同 run 对同一 workspace 的写入仍受已有 workspace 独占策略约束；run owner 不是跨 run 工作区锁。

## 4. 活动资源登记与取消协议

在派发或启动前写入 `ActiveResource`，完成后以可信收据置终态：

```text
ActiveResource:
  resource_id, kind: plan_attempt | agent_task | sandbox_call
  task_id, run_id, generation, parent_resource_id?
  plan_id?, node_id?, attempt_id?, call_id?
  state: registered | running | cancel_sent | exited | cleanup_failed | uncertain
  receipt_ref?, receipt_checksum?, error_code?, updated_at
```

登记仅保存可信资源身份和引用，不以旧 PID、线程对象或 Subagent 活对象作为 checkpoint 可恢复句柄。`parent_resource_id` 用于由 Plan attempt 找到其子 AgentTask 与 sandbox call。Subagent 首期仅覆盖该 L2 路径实际使用的 `AgentTask`；adapter 需要取消协作信号、等待终态和超时后确认不可再提交。沙箱资源通过 call_id 向 supervisor 请求 TERM/宽限/KILL，最后核对 cleanup 收据。Plan node attempt 根据子资源和自身 journal/收据判终态，不重复实现工具副作用判定。

取消请求先 durable 写 `cancel_requested`，原因与请求 ID；RunCoordinator 同步关闭新节点和工具派发，再 durable 写 `cancelling` 并向所有已登记活动子资源发送取消。取消和清理操作按 `(run_id, resource_id, request_id)` 幂等；重复 cancel 返回当前进度，不重新发起相同副作用。每项资源有有界宽限、终止和核对阶段，结果是 `confirmed | failed | unknown` 加稳定错误码、耗时与 receipt ID。父级完成要求所有子级已确认终态；未开始节点交由 Plan reducer 标 cancelled。

有副作用 attempt 在取消中即使进程已退出，仍要由 Plan 恢复器核对收据和工作区后态。只有资源清理确认、活动 Action 结果已核验、Plan/任务状态可持久提交时才标 `cancelled`。残留资源、清理收据不完整或写入结果未知时标 `recovery_required`，保留资源清单并阻断后续派发；不能把协作 token 已置位当作取消成功。

取消过程中的控制器崩溃后，新 owner 取得 `reconciling` 权限，按 durable resource registry 继续清理和核查，不自动重新启动原工具。对其他 run 的资源没有取消权限；资源身份不匹配时拒绝清理，避免误杀。

## 5. 持久化、checkpoint 与恢复接入

RunOwner 和 ActiveResource 元数据与 Plan journal 使用同一受信任控制面，采用原子更新/校验与单调序号。checkpoint seal 只加入 owner generation、取消阶段、活动资源清单引用/checksum、最近协调事件水位；Plan、Action、Observation 与工作区版本仍按各自既有权威存储校验。活动资源引用不得被最近 100 条的历史窗口裁掉。Canonical Trace 记录同 ID 的审计事件，但不能代替 durable registry。

恢复顺序固定为：

1. 获取 run 级互斥并用 CAS 申请新 generation；未得权返回当前 owner/状态，不开启第二个执行循环。
2. 进入 `reconciling`，校验 run/workspace/Plan/checkpoint/journal 身份及单调水位；对旧活动 AgentTask 和 sandbox call 逐项确认停止或隔离。
3. 调用 Plan DAG 恢复器核对 attempt、工具收据和工作区副作用；`uncertain` 的写入阻断依赖节点。context/Observation freshness 由前述上下文规格处理，本版只消费其恢复判定。
4. 取消请求已 durable 存在则继续取消清理；否则仅在所有旧活动资源已核查且安全时切换 `active` 并开放派发。
5. 记录恢复/取消报告和 checkpoint；拒绝旧 generation 的迟到提交。checkpoint/registry 不一致时保持 `recovery_required`，给出缺失引用。

旧 checkpoint 缺 owner/resource 字段时不能声称通过本版并发恢复保障；进入明确的旧模式或拒绝该 profile 自动恢复，不为历史记录补造清理成功。`recovery_required` 不是 `cancelled` 的别名，主 Agent 必须看到残留资源、未知 Action 或身份冲突的具体诊断。

## 6. 事件、错误码与验收

沿用 Canonical Trace，至少发 `resume_owner_acquired/rejected`、`resume_reconciling`、`resume_activated`、`stale_generation_rejected`、`cancel_requested`、`resource_cancel_sent`、`resource_cleanup_confirmed/failed`、`cancel_completed`、`recovery_required`。包含 task/run/generation/Plan node/attempt/call/resource、请求 ID、结果、耗时与稳定错误码；默认不含完整命令、源码或凭据。建议错误码：`resume_owner_conflict`、`stale_generation`、`old_execution_unconfirmed`、`resource_identity_mismatch`、`resource_cleanup_failed`、`action_uncertain`、`coordination_integrity_failed`。

| ID | 场景 | 必须观察到的行为 |
|---|---|
| R1 | 两个 CLI/进程同时 resume 同一 run | 只有一个 owner 获得可派发 generation；另一个返回 owner/状态，不启动工具。 |
| R2 | 旧 owner 租约过期、旧进程仍在 | 新 owner 先 reconcile；旧执行未停止时不能进入 active；旧 owner 迟到提交/派发被拒绝。 |
| R3 | 取消与节点派发竞争 | durable cancel 后不再产生新派发；已登记调用进入清理或结果核验。 |
| R4 | Plan attempt → AgentTask → sandbox call 的取消 | 父子资源逐项可追踪；正确终止并有完整收据才标 cancelled。 |
| R5 | 子进程不退出、Subagent 不响应、控制器取消中崩溃 | 报 recovery_required 与残留资源；新 owner 续清理，不误杀其他 run。 |
| R6 | 写入已发生但取消前无可信终态收据 | Plan attempt 保持 uncertain，后续依赖阻断，不自动重放。 |
| R7 | 重复 cancel、resume、收据与迟到结果 | 幂等；不重复推进 Plan、启动工具或执行副作用。 |
| R8 | checkpoint/registry/generation 损坏或身份不符 | 拒绝自动续跑并报告稳定原因。 |
| R9 | 真实 L2 修复路径 | 正常取消、一次中途崩溃恢复、一次并发 resume 有实际 Plan/sandbox 收据和工作区后态。 |

R1/R2/R3/R5/R9 至少使用跨进程故障注入和专用临时 workspace；单元 mock 不能证明进程退出或并发互斥。保留 run owner 记录、Plan journal、sandbox 收据、checkpoint、工作区 diff 与实际派发次数。只报告实测重复副作用、残留资源、恢复结果和耗时，不宣称 exactly-once、绝对进程隔离或未经验证的性能提升。配套 [开发计划](../plans/2026-09-30-resume-cancel-coordination-mvp.md)。
