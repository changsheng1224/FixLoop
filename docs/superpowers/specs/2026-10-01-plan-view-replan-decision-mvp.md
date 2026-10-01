# Plan MVP：统一计划视图与重规划决策入口

日期：2026-10-01。状态：已按用户确认范围实现并通过相关验收；实际实现、P1–P7 对账、实时阶段与恢复边界见 [验收记录](../../PLAN_VIEW_REPLAN_ACCEPTANCE_2026-10-01.md)。本文下列条款保留为设计契约，不表示其余领域已实施。

## 1. 目标与范围

本轮只交付两项增量：让其他模块消费同一份计划视图，以及用小型决策入口判断是否需要重规划。复用现有 PlanSession/reducer/journal、失败分类、Orchestrator 重试、回滚和安全提交；不从零建设 Plan 系统。

决策入口首版接通**已有修复流程中确认的代码层验证失败**。新探索证据、假设被否定和用户修改目标的自动重规划留待分别讨论；本轮不扩大 DAG、增加独立 Planner、支持运行中任意改图或改写恢复状态机。其他领域的 MVP 范围仍待逐项确定。

整体借鉴与取舍见 [Agent 设计参照](../../AGENT_DESIGN_REFERENCES_2026-10-01.md)。本轮对应其中 Plan 的职责与状态投影；安全执行仍采用 FixLoop 自有契约。

## 2. 已有基础与缺口

- [PlanSession](../../../agent_runtime/plan_runtime/session.py)已有不可变 Plan 快照、reducer 和持久 journal。
- [长任务状态](../../../agent_runtime/plan_runtime/long_task.py)投影当前节点，节点 definition 不包含执行 status/failure；[repair binding](../../../src/repair/plan_binding.py)另外生成 plan_progress。消费点需收敛到同一快照来源。
- [replan](../../../agent_runtime/plan_runtime/replan.py)已有静止点、身份、活动 attempt、写入历史和次数门禁；候选校验后原子提交。
- repair binding 的 retry 分支目前主要按 edit 失败/阻塞/取消或已回滚触发，reason 为 `orchestrator_retry`；[retry_plan](../../../agent_runtime/plan_runtime/planner.py)的模型输入主要是目标与源码 Observation，未显式携带旧计划与结构化验证失败反馈。

以上为静态审计结果；原有验收边界见 [PLAN_DAG](../../PLAN_DAG.md)与 [验收记录](../../PLAN_DAG_ACCEPTANCE_2026-09-30.md)，本次没有重新运行。

## 3. 统一只读计划视图

新增或收敛一个 `PlanSession` 只读投影入口，具体名称实施时确定。从同一个已校验 Plan 快照生成；不维护另一份可独立写入的节点状态，也不通过 Todo 或历史摘要推断当前计划。

最小视图字段：

```text
PlanView:
  task_id, run_id, workspace_id, plan_id
  plan_version, state_revision, plan_checksum
  selected_node_id?, active_node_ids[]
  nodes[]:
    node_id, kind, objective, status
    completion[], depends_on[], dependency_statuses
    input_evidence_refs[], output_evidence_refs[]
    block_or_failure_reason
```

活动节点可能有多个。模型上下文消费点由可信调用方明确指定节点，进度视图可显示全图；不能随意取第一个活动节点。引用只表明关联关系，不自动证明证据 fresh 或完整；沿用已有 EvidenceLedger 校验。

消费范围限于三个已有入口：

1. 长任务/模型上下文：当前节点目标、完成条件、依赖和证据引用来自 PlanView；原始请求和硬约束仍来自 LongTaskState。
2. 工具准备：使用同一身份与版本关联当前 operation，沿用既有派发门禁；显示视图本身不授予执行权限。
3. `plan_progress`：由相同快照派生节点状态，事件通知不能直接改 Plan。

视图是带 revision 的快照，不保证跨多个执行时刻不变。owner 合法推进状态后重建视图；工具派发时检测过期身份/版本并重新核对，不能把旧展示数据当作当前执行依据。新增能力先接入真实 L2 repair，普通 L1 的独立 Todo 保持明确边界。

## 4. 小型重规划决策入口

决策函数不调用模型、不修改 Plan，输入来自可信 runtime：当前 PlanView、失败节点/attempt、验证分类及收据引用、当前工作区版本、现有 retry/stop-loss/deadline/budget 状态、回滚/清理结果和新鲜源码证据引用。

| 条件 | 返回动作 | 后续处理 |
|---|---|---|
| 没有已确认的代码层验证失败 | `keep_plan` | 保留现有流程，不因普通新 Observation 改图 |
| 环境、权限、空测试收集或无法明确分类 | `keep_plan` + 稳定原因 | 交已有环境/失败处理；不是继续写入的授权 |
| 活动执行、uncertain、清理或回滚未确认 | `block` | 沿用协调/恢复规则；不生成候选 |
| 已止损、deadline 或重规划预算耗尽 | `stop` | 保留 Plan/证据，走已有终态处理 |
| 确认代码验证失败，但缺少新鲜源码证据 | `needs_evidence` | 由已有授权读路径重取，再在安全点评估；不暗中执行工具 |
| 确认代码验证失败，重试获准且安全门禁通过 | `replan` | 用增强输入调用现有规划模型，再校验和提交 |

安全/取消/不确定门禁优先于分类，止损/预算优先于生成候选。动作附带短 reason、trigger_ref、Plan identity/revision 和证据引用。代码失败是通用运行时类别，不匹配特定项目、测试名称或错误文本来决定补丁。

Orchestrator 继续决定是否重试并按已有策略回滚；入口只判断该次重试是否允许重规划，不再发起第二轮 retry，也不自动取消活动节点。无前述验证触发的旧错误路径保留现有诊断/处理，不能偷偷变成另一套自动重规划入口。

## 5. 重规划输入与提交

生成候选前先执行已有安全、预算和次数检查，避免已知不能提交时消耗模型调用。首次确认代码验证失败也可触发，不要求失败多次；已有止损仍限制重复无进展。

规划输入在现有 grounded_plan 输入上增加旧计划视图、触发节点、失败验证收据/短反馈、当前 workspace 版本和新鲜源码证据。验证反馈来自本次实际工具执行，禁止使用评测答案性信息。失败发生时的 workspace/attempt 与回滚后的当前版本分别记录：前者证明历史执行事实，不冒充后者的 fresh 源码证据。

模型判断需要修改哪些假设、探索或后续步骤；runtime 继续校验节点数、依赖、完成条件、可信工具权限和写入历史。新语义使用新节点 ID。候选无效时保留旧图与诊断，不部分提交；既有最多两次重规划限制保留。

每个触发以 run + 原 Plan version + 验证 attempt/receipt 形成幂等键，决策/处理结果记入现有 journal，并关联最终 `replan_committed`。重复回调或恢复重读已处理触发不得再次提交候选。每个触发首版只进行一次模型规划尝试，拒绝后不自动无限重试；中断且未对账的处理先按既有恢复规则核查，不把日志缺失当作未执行证明。此处不承诺模型调用 exactly-once。

## 6. 实施拆解与工作量约束

1. 先实现纯投影并接入上述三个消费点；复用现有字段和 evidence API，不增加数据库或模型调用。
2. 再实现小型决策函数并接入验证失败到 retry 的已有边界；复用 `verify_diagnose` 和 stop-loss，不新建分类器。
3. 补重规划输入和 journal 关联，复用既有候选校验/提交，补直接相关回归。

主要模块为 `agent_runtime/plan_runtime/session.py`、`long_task.py`、`planner.py`、`src/repair/plan_binding.py` 与必要的现有 verification/retry 接入。决策的 repair 分类装配归 Layer 2，通用 Plan 视图和版本校验归 Layer 1；Layer 1 不导入 `src`。

如实施时发现要改写 AgentLoop、增加通用触发调度器或扩展 checkpoint schema，本 MVP 停在已明确的边界并先记录额外依赖，不把其他领域顺带纳入。具体差量/工期在实施前核对，本次只确认设计范围。

## 7. 验收

| ID | 场景 | 必须观察到的行为 |
|---|---|---|
| P1 | 相同 Plan revision 的上下文/工具准备/进度 | 节点、版本、完成条件来自同一快照；投影不改权威状态 |
| P2 | 并行活动节点、owner 推进或重规划后旧视图 | 明确 selected node；旧视图不授权当前派发，重新核对版本 |
| P3 | 确认代码验证失败后 retry | 模型收到旧计划、失败反馈与新鲜证据，合法候选提交一次 |
| P4 | 环境失败、零测试、分类不明确 | 不自动请求重规划，不误当作代码问题 |
| P5 | 活动资源、未知写入、回滚未确认、取消 | 阻断候选生成和冲突派发，沿用既有恢复/取消状态 |
| P6 | 证据过期、预算耗尽、止损 | 重取/停止有稳定原因，不静默绕过门禁 |
| P7 | 重复触发、候选非法、恢复重读触发 | 不重复提交；非法图保留旧版；次数/模型尝试有界 |

补充已有 Plan runtime、L2 binding、context/progress 的直接相关测试；至少一条受控 provider 驱动的真实 L2 编辑→pytest 失败→已有回滚→重规划→再次验证路径。区分实际模型请求输入、真实磁盘/工具行为与在线模型效果，不声称修复率提升。相关测试/lint按 CLAUDE.md 执行，全量测试须另行授权。
