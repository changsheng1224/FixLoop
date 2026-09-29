# FixLoop 并发恢复互斥与取消清理闭环 MVP 开发计划

日期：2026-09-30。依据：[MVP Spec](../specs/2026-09-30-resume-cancel-coordination-mvp.md)。状态：未实现。

## 前提、工期与改动原则

顺序 P0 → P1 → P2 → P3 → P4，预计单人 6–10 个有效工作日，含相关测试和集成返工。前提是 Plan DAG durable attempt journal/恢复器及 WSL supervisor cancel/reconcile/receipt 已在同一条 L2 修复路径实现。若前提缺失，先交付并验证对应前置项目；本计划不得以会话内 Action Ledger、CancellationToken 或 mock supervisor 代替生产契约。

当前工作树有未提交变更。开始时记录涉及文件状态/哈希，不 reset/stash/覆盖。`CLAUDE.md` 规定相关测试和分支/PR 工作流；不自动运行全量测试、push 或合并。对已有恢复器和沙箱只做必要接口接入，不复制状态机或进程管理逻辑。

## P0：接口核对与竞争 fixture（1 天）

1. 锁定一条真实 L2 修复入口的 task/run/workspace 身份；列出 PlanSession、attempt journal、ToolExecutor、`AgentTask`、sandbox call 与 checkpoint 的实际 ID 映射。
2. 核查 Plan 派发是否有原子 prepare/dispatched 边界，沙箱是否能以 call_id/generation 拒绝过期启动并完成进程清理；找出任何可绕过 gate 的写/测试入口。缺口列为前置修正，不用新增 coordinator 掩盖。
3. 在专用临时 workspace 做两个进程争抢 resume、取消与派发交错、取消中控制器崩溃的确定性 fixture，先标定现有失败行为。

门禁：前置接口真实可调用；并发/清理测试有可复现断点。建议相关测试：`tests/test_checkpoint_resume.py`、Plan DAG 与 WSL sandbox 模块测试（按落地文件名调整）。

## P1：run owner、generation 与派发门禁（2–3 天）

模块建议：`agent_runtime/run_coordination/owner.py`、`store.py`，Plan scheduler/ToolExecutor 的小型 gate 适配；L1 不 import `src`。

1. 实现 RunOwner 的原子 CAS、跨进程互斥、单调 generation、heartbeat 与 `reconciling/active/cancelling/released` 状态。
2. 同 run 重复 resume 返回 owner 状态；旧 owner/过期 generation 在 journal 提交、reducer 更新、收据接纳和工具派发处被拒绝。
3. 派发在互斥保护下 durable 登记并进入受控后端；新 owner 只在旧执行已停止/隔离、Plan attempt reconcile 完成后切 active。协调 workspace 执行锁，防止已启动命令越过数据库 fencing。
4. owner 信息与 checkpoint 关联；恢复身份不符、持久状态损坏和 heartbeat 丢失有稳定错误码。

新增测试建议：`tests/test_run_owner.py`、`tests/test_run_owner_process_race.py`。覆盖 R1/R2/R7/R8。门禁：跨进程只有一个 owner 可派发，旧 generation 的所有提交点均拒绝。

## P2：资源登记与取消流程（2–3 天）

模块建议：`agent_runtime/run_coordination/resources.py`、`cancel.py`，Orchestrator/CollaborationStore/sandbox backend 适配。

1. 派发前登记 Plan attempt、AgentTask、sandbox call 及父子 ID；完成时只凭可信收据置终态，活动引用不被最近 100 条历史裁掉。
2. durable 记录 cancel request 后关新派发；转换 cancelling，向活动资源发送取消并有界等待。沙箱 TERM/KILL/等待及进程身份核查委托 supervisor；Subagent 通过协作任务身份取消/等待，不按裸线程或旧 PID 清理。
3. 将资源结果归并为 `confirmed/failed/unknown`。副作用 attempt 交 Plan 恢复器校验；有残留或未知结果则 `recovery_required`。重复 cancel 不产生重复清理副作用。
4. 取消中控制器崩溃时由新 owner 读取 durable registry 续清理；检查其他 run 资源不会被误杀。

新增测试建议：`tests/test_run_cancel.py`、`tests/test_run_resource_registry.py`，补工具/沙箱取消测试。覆盖 R3–R7。门禁：取消完成意味着清理与副作用状态已确认；token 置位本身不能产生 cancelled 终态。

## P3：checkpoint/恢复接入和事件（1–2 天）

模块：`agent_runtime/checkpoint.py`、`session_contract.py`、Plan recovery 适配、Canonical Trace；L2 Orchestrator 最小接入。

1. checkpoint seal owner generation、取消阶段、活动资源引用/checksum 和协调事件水位；Plan/Action 事实仍从 Plan journal/收据读取。
2. 恢复路径固定为 acquire → reconcile 旧资源 → Plan 在途恢复 → active 或 recovery_required。已有取消请求优先续清理，不能开放新工具。
3. 事件携带稳定关联 ID 与错误码，过滤完整命令、源码和敏感输出；旧 schema 有明确诊断，不伪装已通过清理校验。

新增测试建议：`tests/test_run_coordination_checkpoint.py`，补 Plan 恢复和 trace 测试。覆盖 R5/R8。门禁：checkpoint 后重启能重建正确 owner、资源清单和取消阶段，不生成第二份 Action 真相。

## P4：进程级与真实路径验收（1–2 天）

1. 跑 R1–R9：至少一个真实 L2 正常取消、一个取消中进程级 crash 后恢复、一个跨进程 resume 竞争；记录实际派发次数和 sandbox 子进程清理结果。
2. 核对旧 owner 迟到结果、取消中部分写、残留后台子进程和重复取消；保留 Plan journal、sandbox receipt、workspace diff、checkpoint 与事件样本。
3. 汇总实测重复副作用数、残留资源数、恢复判定与耗时；写明未覆盖平台与已知限制。运行相关测试和受影响文件 lint/format；全量测试须用户显式授权。

完成定义：R1–R9 均有可复核证据，未知副作用不自动重放，取消成功有逐资源清理确认，同一 run 并发 resume 不会形成两条可写执行流。前置 Plan/沙箱若尚未实现，只报告已完成的独立模块，不宣称本 MVP 完成。
