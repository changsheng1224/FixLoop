# 恢复与取消 MVP：严格入口与统一结果

日期：2026-10-02。范围：用户确认的严格恢复入口、恢复/取消结果投影及取消最终结果接入。复用 [RunCoordinator](RUN_COORDINATION.md)、Plan 恢复器、[决策与恢复投影](DECISION_PROJECTION_ACCEPTANCE_2026-10-02.md) 和 [工具批次](BATCH_PREFLIGHT_ACCEPTANCE_2026-10-02.md)，不重建执行状态机。

## 接口与实现

| 位置 | 本轮增量 |
|---|---|
| [Orchestrator.repair](../src/orchestrator.py) | `run_id` 用于新执行，`resume_run_id` 用于严格恢复，两者互斥；严格校验在 owner 获取、trace/task 修改及执行流程之前 |
| [checkpoint_load](../src/repair/checkpoint_load.py) | `require_valid=True` 返回稳定原因错误；核对 envelope/checksum、重复字段、run/task/workspace、任务原文及直接消费的字段形状；修正保存 state_root 的父目录层级 |
| [RepairRunContext / pipeline](../src/repair/run_context.py) | 本次调用持有已校验 checkpoint；流水线只消费明确的恢复输入，新任务不按 run ID 猜测是否加载旧 checkpoint |
| [recovery_outcome](../src/repair/recovery_outcome.py) | 从当前协调快照与 Plan 恢复报告生成脱离源对象的诊断投影，过滤 owner_token、资源 payload、命令与源码 |
| [RepairPlanBinding](../src/repair/plan_binding.py) | 在资源核查、Plan 对账、上下文接入、取消请求与终结边界发布同一投影；接管者与旧 generation 分开诊断 |
| [RepairState](../src/state.py) / [progress](../src/repair/progress.py) / [RepairReport](../src/repair_report.py) | `state.recovery_outcome` 返回副本；`recovery_progress` 事件与 result.json/report.md 消费相同投影，包含中文下一步提示 |
| [RunCoordinator](../agent_runtime/run_coordination/coordinator.py) / [store](../agent_runtime/run_coordination/store.py) | 清理失败并释放 owner 后，同 generation 重复取消返回持久结果；终结 run 与活跃 owner 竞争使用不同原因码 |

```python
# 新执行，可指定身份；不读取旧 repair checkpoint。
state = orch.repair(issue, run_id="new-run")

# 严格恢复；原始任务须与 checkpoint 一致。
state = orch.repair(issue, resume_run_id="existing-run")
outcome = state.recovery_outcome
```

普通 `load_repair_checkpoint()` 的探测返回契约保留，严格公开恢复显式使用 `require_valid=True`；没有新兼容层、旧 checkpoint 迁移或失败回退路径。缺 envelope 的记录不满足严格恢复要求。checkpoint 校验通过只允许进入 owner/reconcile 流程，不证明当前资源已停止，也不直接开放派发。

## 行为边界

| 情况 | 实际行为 |
|---|---|
| 显式恢复的 checkpoint 缺失、JSON/消费字段非法 | 返回 `recovery_required` 和 `resume_checkpoint_missing/malformed`；不进入执行、不覆盖原文件 |
| envelope/checksum 或重复字段不符 | `resume_checkpoint_integrity_failed`，同上 |
| envelope schema 版本不受支持，即使 checksum 正确 | `resume_checkpoint_schema_mismatch`，不自动迁移或执行 |
| run/task/workspace/state_root 身份不符；原始任务变化 | `resume_checkpoint_identity_mismatch` 或 `resume_task_objective_mismatch`，同上 |
| 有效 checkpoint，但当前 owner 被占用 | `resume_owner_conflict`，提示等待；不改变赢家的 owner、任务或 trace |
| 已 cancelled/failed 的 run 再次恢复 | `resume_run_terminal`，提示使用新 run；不重新执行 |
| 资源未停止、缺适配器或缺可信清理结果 | 保持 `recovery_required`，列出资源 ID/种类/状态/原因/收据引用；保留工作区 |
| 已请求取消 | 投影为 `cancel_requested`；不能显示取消完成 |
| 清理确认，但 `cancel(finalize=False)` 保留 fence 等待既有回滚 | 投影仍为 `cancelling`；下一步等待，不能把内部 CancelReport 的预备结果当作任务终态 |
| 既有清理与回滚完成，协调存储终结 | 投影为 `cancelled`；终结后再使用新 run |
| 清理失败后同 generation 重复取消 | 只返回已持久结果，不再次调用 adapter、不改变资源或协调 revision；重新尝试清理需显式恢复取得新 owner |
| 原 generation 取消时已有接管者 | 仍拒绝，不能利用重复请求路径修改新 owner |
| checkpoint 包含旧成功显示投影 | 恢复时丢弃该投影，按当前资源/journal 重建 |

投影记录 `run_id/generation/coordination_revision`、阶段、结果、原因、取消请求标志、资源与阻断资源、Plan 的 adopted/restarted_read/rerun_verify/stale/uncertain/blocked，以及下一步提示。它是诊断数据，不是第二份 Action 账本，也不是权限凭证；执行仍经过 owner、generation、Executor、Plan 和工作区门禁。

`cleanup_confirmed` 与 `effects_verified` 分开：后者仅表示 **本次 Plan 恢复涉及的 attempt**，其 scope 为 `recovered_plan_attempts`。Plan 报告明确接纳已核验结果时为 true；存在 uncertain/blocked 时为 false；只有进程清理、没有对应 Plan 核验时为 null。它不证明整个修复正确、未来写入安全或所有验证通过。没有资源的 checkpoint/owner 阻断也不冒充清理确认。

取消传播、子到父清理、未知写入保留、回滚时的 cancelling fence、迟到结果拒绝和崩溃后的 Plan 后态对账沿用已有实现。本轮补齐其可消费结果及重复失败取消，不新增强杀线程、任意 PID 清理或自动重试。

## 借鉴与技术取舍

| 参照 | 本轮采用 | 保留或后置 |
|---|---|---|
| OpenCode（R-06/R-10） | 执行控制与展示事件分开，公开结果保留具体原因 | 不增加 HTTP/SSE 服务；事实仍由既有协调存储、Plan journal 与执行收据提供 |
| Pi（R-05） | 取消请求、执行结束及结果归并分别解释 | 不将 abort 信号提升为 OS 清理证明，不引入第二内核或重试调度器 |
| Codex / Claude Code 的会话继续边界对照 | 对话/上下文继续与实际在途执行恢复分开评价 | 严格入口、资源投影与副作用 scope 是 FixLoop 本地设计，不声称上游有相同保证 |
| Hermes（R-08/R-09） | 历史能力可在稳定恢复边界后接入 | 历史搜索、经验提炼与 Skill 发布继续后置 |

来源沿用 [五维对照](AGENT_DESIGN_REFERENCES_2026-10-01.md)，决定见 [ADR-020](design-decisions.md)。本轮没有重新联网研究。保留 Python、现有 L1/L2 分工和存储；展示投影放在 L2，通用重复取消及终结错误区分放在已有 L1 coordinator/store，L1 不导入 src。

首版限定公开 L2 repair 及其已有协调路径；其余会话入口、底层非协调异常的全面归一化、远程恢复和新沙箱后端仍后置。未解除 wsl_bwrap CLI 产品门禁，未重复 WSL 专用验收，也不新增跨存储原子提交或 exactly-once 保证。任务原文采用精确一致性校验，改变目标应使用新任务，不进行自动语义迁移。

## 验证记录

相关节点按最新运行结果去重：**102 passed、0 failed、0 skipped**，其中 [新增行为测试](../tests/test_recovery_cancel_outcome.py) **21 个节点**。受控 provider 驱动实际文件操作、Windows 进程、协调存储和 host pytest；无在线模型调用、无全量测试、无提交/推送或 PR。

| 报告 | 本批结果 | 验证与处理 |
|---|---|---|
| `baseline.xml` | 2 failed | 实现前复现：缺失/损坏 checkpoint 的显式恢复仍抵达普通执行入口 |
| `first.xml` | 1 passed / 7 failed | 结果访问器误放在 SuspectLocation，修正到 RepairState |
| `accessor.xml` | 7 passed | 精确复验恢复零执行、阻断资源/报告、副本隔离、取消三个阶段 |
| `entry.xml` | 30 passed / 3 failed | 入口形状/身份、指定新 run、owner 竞争、持久取消、外部 state_root、恢复/绑定回归；3 个旧测试 double 无 patcher，却调用真实 owner/Plan 初始化 |
| `gates.xml` | 54 passed | 协调竞争、清理 adapter、进度与实际 CLI 生命周期 |
| `process.xml` | 2 passed | 公开正常续跑；实际修复进程在写入结果落盘后 os._exit，再公开恢复→host pytest，写入只发生一次，恢复模型调用为零 |
| `fixtures.xml` | 4 passed / 1 failed | 修正 3 个流水线单元 double 的初始化接线、Plan 投影；新增重复未知清理暴露 owner 已释放后的 stale_generation |
| `repeat.xml` | 9 passed | 修复并精确验证重复取消不重清理；补取消 fence、持久取消、既有回滚顺序和公开取消后回滚 |
| `fence.xml` | 2 passed | 原 generation 重复取消仍被拒绝，旧 checkpoint 显示投影不进入当前状态 |
| `schema.xml` | 1 passed | 正确 checksum 也不能让不支持的 envelope schema 进入执行 |

三项旧流水线测试使用虚拟补丁且没有 Agent，明确隔离 owner/Plan 初始化后，只验证恢复后的反馈、空补丁重试及 critic 流程；真实执行门禁由 process/binding 测试验证，没有为测试放宽生产门禁。外部 state_root 用例还确认 checkpoint 保存的根目录层级正确、读回不改文件。

原始报告、[汇总脚本](../artifacts/recovery-cancel-mvp-2026-10-02/summarize.py) 与 [summary.json](../artifacts/recovery-cancel-mvp-2026-10-02/summary.json) 保留在 [artifacts](../artifacts/recovery-cancel-mvp-2026-10-02/)。按 JUnit 开始时间选择每个节点最新结果，不相加重复通过数；失败历史保留。

受影响的 16 个 Python 文件 Ruff check/format check 通过；相关 tracked diff check、94 个本地文档链接、新增文件与相关文档空白检查通过。两个改动的 L1 协调模块 AST 导入检查无 src 依赖。本记录只覆盖本轮增量，不把旧规格 R1–R9 或所有后端整体标为重新验收。
