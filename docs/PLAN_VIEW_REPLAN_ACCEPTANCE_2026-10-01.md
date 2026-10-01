# Plan MVP 实现与验收

日期：2026-10-01。范围：[已确认规格](superpowers/specs/2026-10-01-plan-view-replan-decision-mvp.md)的统一视图与验证失败重规划入口。实现和相关验收完成；其余领域仍按 MVP 分别讨论。

## 实现与借鉴

- 延续 OpenCode 的角色/权限/事件组织：`PlanSession.plan_view()` 从已校验 Plan 生成带 identity/version/revision/checksum 的脱离源对象的投影。上下文、工具准备、进度均消费该投影；展示数据不能改 reducer 或授权执行。普通 L1 Todo 保持独立。
- 延续 Pi 的内核/策略边界：通用投影与派发版本检查归 L1；`src/repair/replan_decision.py` 的失败分类装配归 L2。决策函数无 I/O、无模型调用、无 Plan 修改，返回 `keep_plan/needs_evidence/replan/block/stop`。
- 继续使用 FixLoop 的证据、收据、回滚、协调与重试。首版只有确认的代码验证失败可自动重规划；环境、零测试、未知失败均保留计划。其他错误路径保留诊断，不能另行自动改图。
- 模型收到旧视图、失败节点/收据引用、短失败日志、失败时 workspace、回滚后 workspace 和新鲜源码引用。失败工作区是历史事实，不能替代当前源码。候选仍受已有节点/依赖/工具权限校验；非法候选不再退回默认图后提交。
- 原始请求初始化进入现有 LongTaskState；上下文投影不再暗中改 stale_evidence。没有增加决策版本系统、通用事件调度器、数据库表或 checkpoint schema。

上游来源和未采用的设计见 [五维对照与技术取舍](AGENT_DESIGN_REFERENCES_2026-10-01.md)。Hermes 的历史检索/经验补充仍后置，本轮没有扩大到该领域。

## 提交与恢复边界

生成前检查取消、活动/uncertain、协调资源清理、最新一次回滚和 workspace、止损、deadline、共享模型预算及最多两次重规划限制。缺证据时通过已有固定 explore 节点重读，沿用 read budget；预算拒绝发生在改节点状态之前。模型返回后复验源证据、Plan checksum 和安全状态，再提交。

幂等键由 run + 原计划版本 + 验证 attempt + 验证 evidence receipt 引用计算。现有 journal 记录 `replan_request` 的 started/committed/rejected；Plan.replan_reason 与 `replan_committed.trigger_ref` 关联同一触发。每个触发仅一次候选模型尝试，拒绝后不能重复请求。

保存 Plan 后、写 trace 前中断：恢复从带 parent checksum 和触发原因的已保存 Plan 对账提交，补一次缺失通知。只有 started 且没有提交证明：返回 `replan_attempt_outcome_unconfirmed` / recovery_required，不把缺日志当作未调用模型，也不自动再请求。此 MVP 不承诺模型 exactly-once；没有新增人工对账 UI 或跨宿主机恢复协议。

## 实时阶段

沿用默认启用的 `ProgressEmitter`，控制台即时 flush，事件名 `plan_progress`，无需新增服务。示例阶段顺序：

```text
plan: planning_started
explore: node_started / node_succeeded
analyze: node_started / node_succeeded
edit: node_started / node_succeeded
verify: node_started / node_failed
replan: replan_decided / replan_started / replan_committed
explore → analyze → edit → verify
```

失败/阻断/拒绝如实输出；JSONL/record 中带 phase、只读 plan_view、action/reason/trigger_ref。`FIXLOOP_PROGRESS=0` 沿用静默设置；`FIXLOOP_PROGRESS_JSONL` 沿用现有结构化输出。进度通知不改变权威状态。

`state.node_timings.plan_progress` 也从相同视图生成；保留现有契约的 version/id 展示字段作为派生别名，未新增可独立修改的执行状态。

## P1–P7 对账

| 要求 | 直接证据与观察结果 |
|---|---|
| P1 同 revision、纯投影 | `test_view_context_and_tool_share_snapshot_without_mutation` 对比 context 与 operation 的完整视图；`test_stale_context_projection_leaves_authoritative_state_unchanged` 核对 stale 投影不改状态/journal；L2 测试对比 progress 与当前视图 |
| P2 并行/过期 | `test_parallel_context_requires_selected_node_and_rejects_old_view` 显式绑定并行节点，拒绝过期 revision、已结束 attempt；L2 重规划后拒绝旧版本视图 |
| P3 真实失败重试 | `test_failed_verifier_rollback_and_bounded_replan` 实际 patch_file→host pytest 失败→既有回滚→一次模型规划→再次 pytest 成功；检查实际模型输入和实时阶段 |
| P4 非代码失败 | 纯决策参数场景与 L2 `test_non_code_failures_never_request_model` 证明 env/零测试/unknown 不增加模型调用或提交请求 |
| P5 安全优先 | 取消、uncertain、最新回滚未完成、未清理资源测试均在生成前拒绝；模型返回后新 uncertain/外部源码变化也不能提交；已有恢复/协调回归通过 |
| P6 freshness/预算/止损 | stale blob 通过授权读重取；模型预算、deadline、止损和 read budget 拒绝有明确原因且没有额外模型调用；纯入口和已有 runtime 保持重规划次数上限 |
| P7 去重/拒绝/恢复 | 非法图保留旧 Plan、恢复后不再次调用模型；未对账 started 阻断；保存候选后 trace 前中断从 durable Plan 补齐提交，不重复提交 |

## 实际验证记录

仅运行直接相关测试，未运行全量测试。首批 23 项中 22 通过、1 项 tuple/list 投影格式失败；统一 JSON 字段形式后精确复验通过。L2 边界首批 12 项中 9 通过、3 失败；最新回滚预检修正及两个测试夹具问题修正后，三个失败 nodeid 精确复验通过。后续改动只补跑受影响的提交路径及新增场景，未无理由重跑整批。

| 批次 | 结果 | 持久结果 |
|---|---|---|
| Plan runtime/crash、L2、native batch integration、coordination binding、ContextManager、repair progress | 113 passed | [regression.xml](../artifacts/plan-mvp-2026-10-01/regression.xml) |
| 提交复检、实时进度/输入、stale 重取、提交窗口恢复、真实 pytest 失败重规划 | 6 passed | [commit-gates.xml](../artifacts/plan-mvp-2026-10-01/commit-gates.xml) |
| 非代码失败与 stale 投影纯度 | 4 passed | [classification.xml](../artifacts/plan-mvp-2026-10-01/classification.xml) |
| 读取预算拒绝不改图/不请求模型 | 1 passed | [read-budget.xml](../artifacts/plan-mvp-2026-10-01/read-budget.xml) |

上述批次有用例重叠，不能相加作为独立用例数。受控客户端证明请求输入/调用边界；真实磁盘编辑和 host pytest 证明执行链路。本记录不证明在线模型修复率、WSL repair profile 或发布级全量通过。

修改的 11 个 Python 文件通过 `ruff check` 和 `ruff format --check`；`git diff --check` 通过。工作区已有文档和未跟踪内容保留；未提交、推送或创建 PR，先前创建分支因 `.git` 写权限不足未完成。
