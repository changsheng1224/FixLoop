# FixLoop Plan DAG 与完整在途任务恢复 MVP Spec

日期：2026-09-30。状态：已实现并验收选定的 Python/host 修复路径；实现与恢复边界见 `docs/PLAN_DAG.md`，实跑证据见 `docs/PLAN_DAG_ACCEPTANCE_2026-09-30.md`。本文保留为开发契约。

## 1. 目标与范围

将当前线性 Todo 提升为有依赖、可核验完成条件、证据引用和持久执行记录的小型任务 DAG。主 Agent 基于有预算的仓库探索生成计划；最多两路固定只读工具可并行，分析、修改和验证由主 Agent 串行推进。新证据可在静止点更新计划，保留旧版本。

完整在途任务恢复是本版必须交付的能力：恢复器覆盖节点派发前、执行中、工具结果已落盘但 Plan 尚未更新、Plan 已更新但 checkpoint 尚未提交等中断点；核对执行收据、Observation 和工作区后继续安全的节点。无法确认副作用时保持 `uncertain`，暂停相关下游，不自动重放。

不做当前 Todo 与 DAG 的对照实验。不以更快、更少 token 或修复率提升为验收结论；使用确定性 fixture、故障注入和至少一个真实修复流程证明契约。

规划工作量约 18–28 个有效工作日，含相关测试与集成返工；约 1,800–3,000 行实现加测试仅作范围估计。恢复能力所需的 durable journal 和故障注入是主要增量。

## 2. 非目标与接入边界

- 不建通用工作流引擎，不支持任意条件表达式、循环边、动态无限节点或并行修改。
- 不启动独立 Planner Agent 或并行 LLM 推理循环。规划由主 Agent 的一次受约束输出或既有 light client完成。
- 不做执行中任意图编辑。重规划只在旧活动节点已进入可核查终态的静止点提交。
- 不做跨任务 Plan 缓存、全仓 Code Graph、模型效果对照、可视化 UI。
- 不要求原进程从中断指令继续运行；完整恢复的是执行事实、节点状态和后续调度。
- 不依赖前述代码检索 MVP 或 WSL 沙箱 MVP 已实现；有这些能力时复用其证据/收据，但不得伪造缺失契约。

当前 L1 `AgentLoop._plan_phase` 使用 `plan_todos`，在成功工具调用后 `_advance_todo`，不检查步骤条件。L2 Patcher 调用 `agent.ask(..., skip_plan=True)`。本版主要接入**一条真实 L2 修复路径**，通过 L2 编排器提供任务入口；不得只改普通 L1 对话 Todo 然后声称修复流程已接入。

L2 Orchestrator 保留修复重试、验证和终态的现有职责；Plan 服务负责小型任务依赖、证据验收与进度，不再创建第二个补丁重试控制器。L1 通用 Plan 模块不能 import `src`。

当前 `_plan_phase` 在 Agent 正式工具循环前调用，不能直接满足“探索后规划”。本版为选定 L2 修复路径增加一个受限的 pre-plan 探索阶段：主 Patcher Agent 仅使用注册表核定的只读工具，最多 4 次调用/固定预算，工具结果先入现有 Observation；随后以这些证据生成/验收 Plan，才开放修改。无足够证据时只可生成含待验证探索节点的小计划。其他 L1 `Agent.ask()` 路径不强制进入该模式。

一条修复 run 持有一个 PlanSession。Patcher 的探索、分析与编辑节点由同一 Agent 执行；Plan 的 verify 节点由 Orchestrator 将现有 Verifier 的实际收据/结果回灌，不再额外运行一遍 pytest。Orchestrator 的重试会作为重规划触发或终态，而非与 Plan 各自重试同一个补丁。PlanSession 的任务身份沿用现有 L2 repair run，不能用单次 Patcher ask 的临时 run ID 偷换。

已发现 `ToolDAGExecutor.run` 在标记失败依赖后只从 pending 移除未阻塞的 ready；复用前必须以失败依赖测试复现并修正，否则可能循环。当前 ToolDAGExecutor 除测试外未发现生产调用。它的只读并发/副作用串行原则可复用，不能直接复用共享可变 ToolContext 的执行回调。

## 3. 模型与版本

建议新增 `agent_runtime/plan_runtime/`，包含 `models.py`、`validate.py`、`reducer.py`、`scheduler.py`、`journal.py`、`recovery.py`、`evidence.py`。L2 只做计划入口和修复状态映射。

```text
Plan:
  schema_version="1", plan_id, plan_version, state_revision
  task_id, run_id, workspace_id, session_id, created_at, status
  nodes[], plan_checksum, parent_plan_checksum?, replan_reason?, replan_evidence_refs[]

PlanNode:
  node_id, kind: explore | analyze | edit | verify
  objective, depends_on[], tool_allowlist[], side_effect: read | write | verify
  completion: typed predicate list with all-of semantics
  input_evidence_refs[], output_evidence_refs[]
  status: pending | ready | running | succeeded | failed | blocked
        | cancelled | stale | uncertain
  attempt_id?, started_at?, ended_at?, failure?, receipt_refs[]

NodeAttempt:
  attempt_id, plan_id, plan_version, node_id, state_revision_at_dispatch
  workspace_before, allowed_tools[], idempotency_key
  phase: prepared | dispatched | result_recorded | reconciled
  tool_call_ids[], receipt_refs[], observation_refs[]
  workspace_after?, terminal_status?, failure?
```

`plan_version` 只在图结构、节点定义或依赖变更时递增。节点状态经 reducer 变化时只递增 `state_revision`。结果必须带 plan_version、node_id、attempt_id；旧版本或旧尝试的迟到结果不能写入当前节点。

node_id 在同一计划谱系中稳定；新语义节点生成新 ID，不能用原 ID 偷换完成条件。旧计划版本及结果记录追加保存，不能原地覆盖。单次计划节点数最多 8，每节点依赖最多 3，最多 2 条只读并行支路；分析、修改、验证节点由主 Agent 一次处理一个。

## 4. 完成条件与证据

完成条件只用可编程判断的有限类型，模型提供值，运行时验证：

| 条件类型 | 成功判据 |
|---|---|
| observation_present | 指定任务作用域的 Observation ID 存在、checksum 有效、依赖版本可校验 |
| analysis_recorded | 主 Agent 给出结构化结论与有效证据 ID；只证明记录充分，不保证分析一定正确 |
| patch_applied | 可信工具收据有受影响文件、调用已终态、变更与工作区版本相符 |
| tests_passed | 指定验证收据属于该 task/attempt，测试命令完成且状态为通过 |

主观条件如“分析完成”“修复正确”不能作为唯一完成条件。模型可以附文字说明，但 reducer 不将其当作已满足。验证失败、环境错误、无测试、未知执行须分别表达，不能都映射为 failed assertion。

Observation 引用只存 ID、checksum、源路径及版本/依赖摘要；不复制完整源码。既有 ObservationStore 的路径依赖不一定覆盖目录搜索和跨文件关系；不能仅凭 `source_version` 字段等于工具版本就称文件新鲜。无法校验的证据标 `unknown`，不用于自动确认完成或复用。

计划产生前主 Agent 先有预算探索。Planner 输出的路径/符号若无 Observation 支持，标为假设并安排 explore 节点，不作为已确认文件关系。计划可以从已有文件/文本工具证据起步；代码检索 MVP 是可选增强。

预规划探索本身也可能在途崩溃：它纳入同一 task/run 的 pre-plan attempt journal。恢复时先核对这些只读调用，安全终止旧执行后可重做；没有 Plan 的 checkpoint 不可被误判为“无需规划而直接开放写入”。

## 5. 校验、reducer 与调度

接受计划前验证：身份、schema、checksum、节点/依赖唯一、无环、每节点 typed completion、工具名在注册表、side_effect 不低报、只读节点工具确实只读、节点/预算上限。

只读工具白名单由可信注册表决定，模型声明不能把 `run_shell`、`quick_test` 或能执行仓库代码的工具降为 read。分析不作为并行模型节点；verify 可能写缓存与产生进程，首期按有副作用串行处理。

状态 reducer 是唯一状态写入接口。核心转换：

```text
pending → ready（所有依赖 succeeded 且所需证据新鲜）
pending/ready → blocked（依赖 failed/cancelled/uncertain，或证据不满足）
ready → running（先持久化 attempt，再派发）
running → succeeded（完成条件和收据成立）
running → failed | cancelled | uncertain（根据终态证据）
succeeded/pending/blocked → stale（依赖事实失效；历史成功保留在旧 revision）
stale → ready（新计划/reducer 重新验收后，不默认重跑旧修改）
uncertain → succeeded | failed | blocked（恢复核查后有充分证据）
```

同一节点不可同时有两个活动 attempt。依赖结束不等于成功；失败/不确定向下游传播 blocked，但保留每个下游原因。工具调用返回 success 仅为一个输入，只有 typed completion 满足才能 `succeeded`。

调度器一次提交最多两个互不依赖的固定只读工具操作，各自独立 ToolContext/cancel token/预算子额及 Observation 归属。主线程汇总结果后再经 reducer 提交；共享总预算和 deadline 必须原子扣减。并行时工作区写锁持有者不能启动，发现外部版本变化则拒绝提交旧证据。

写入由主 Agent 持有执行权并串行；执行前再检查依赖和工作区版本。写后记录实际变更，标记使用旧版本的分析/探索结果与未执行下游为 stale/blocked。已成功修改节点是发生过的历史事实，不能因为其前置读证据被自己修改过就自动重放补丁。下游重新检查当前文件和验证证据。

## 6. 局部重规划

本版支持**静止点的有界局部重规划**。触发包括新探索证据、失败验证、依赖失效、任务目标变更。输入是当前计划、触发 node/证据、工作区版本和仍有效的已完成节点。

有活动节点时先取消并等待结果/清理；无法核查副作用时停在 uncertain，不提交可能与它冲突的新计划。replanner 输出候选 Plan，先完整校验并原子提交新 plan_version，再开放调度。旧版保留 checksum、时间、触发证据和节点差异。

可沿用的成功 explore/analyze 必须证据仍新鲜、目标与条件不变。已完成 edit 只作为有收据的历史操作沿用，不自动再次派发；其输出版本需与当前工作区核对。被替换/失效节点及其依赖后继需要重新验收，不能静默沿用 ready 状态。

重规划最多 2 次/任务，超出返回明确 budget 状态并保留当前计划；不降成无限生成循环。非法候选保留旧版及诊断，绝不部分提交。简单顺序计划降级也必须通过同一校验器。

## 7. 持久化与完整在途恢复

安全恢复需要 checkpoint **加 durable attempt journal**。现有 checkpoint 在工具成功步或 ask 结束形成，不能单独覆盖“派发后、成功 checkpoint 前”崩溃窗口。计划与 attempt 不把 Canonical Trace 当作唯一真相。

持久化次序：

1. 在 session/PlanStore 原子保存 plan_version/state_revision，派发前写 `prepared` attempt 与输入版本、工具调用身份。
2. durable 标记 `dispatched` 后才调用工具；绑定原有 ToolExecutor call_id、action ledger、receipt。
3. 收到工具结果先保存原始收据引用、Observation ID、变更清单与终态；再由 reducer 判完成条件，保存新 state_revision。
4. checkpoint seal 包含 Plan 内容或稳定引用、checksum、revision、活动 attempt manifest、最近 journal 序号，并检查反向关联。

原子写入与 crash-consistency 需要在当前 Windows/目标文件系统上验证；文件和目录持久化能力不足时记录限制，不夸称断电持久。journal 与 checkpoint 采用受信任存储，命令/模型不能篡改；保留版本历史与必要收据，不能被现有 100 条截断窗口默默丢掉活动记录。

恢复算法：

1. 校验 task/run/workspace/session、Plan schema/checksum、DAG、checkpoint envelope、journal 单调序号及 attempt 身份。
2. 以 journal/receipt/Observation/文件版本重建最后可信 revision。checkpoint 落后但 durable result 已完成时可前滚；不能只看最后一个 Todo 文本。
3. 对活动调用先确认旧执行停止/取消，或与当前恢复者隔离，避免同一 workspace 并发写。现有线程超时不等于进程树停止；无法确认时保持 uncertain 并阻止冲突操作。
4. 只读 attempt：确认旧调用结束后可重做，按新 attempt ID 记录；旧迟到结果作废。
5. edit attempt：可信成功收据、Observation 与文件后态一致时接纳完成；可信失败且无副作用证据时可标失败。收据缺失、部分写或冲突时 uncertain，先记录实际 diff 并交主 Agent 决策，不自动重放。
6. verify attempt：可信完整测试收据可接纳；旧进程终止且收据不完整时新建 attempt 再测试，旧中间输出不当作通过。
7. 已完成节点重验完成证据和工作区版本；过期派生证据只使相关节点及后继失效。写节点的历史副作用不因此自动重复。
8. 生成恢复报告：adopted、restarted_read、rerun_verify、stale、uncertain、blocked、resume_rejected 及关联证据，安全 ready 节点才能续跑。

“完整”指上述全部中断窗口和状态判定都有实现与测试，**不表示 exactly-once 执行**。任何无法证明的写入结果必须保持 uncertain；不能借“恢复”之名继续潜在冲突执行。计划损坏、schema 不兼容、身份不符时拒绝自动续跑并保留诊断。

## 8. 事件与展示

沿现有 Canonical Trace 发 plan_created/updated、node_ready/started/succeeded/failed/blocked/stale/uncertain、parallel_reads_started、replan_committed、plan_resume_reconciled。事件包含 task/run/plan/version/revision/node/attempt、依赖、触发原因、证据与收据 ID、时间；默认不含完整源码或私密内容。

用户进度只显示计划阶段、进行节点、依赖等待、已完成/阻塞数量及恢复状态。事件用于可观测性，PlanStore/journal/checkpoint 才是恢复依据。

## 9. 验收矩阵

| ID | 场景 | 必须观察到的行为 |
|---|---|---|
| D1 | 重复 ID、未知依赖、环、超预算、主观条件 | 计划拒绝，有定位诊断 |
| D2 | 只读节点宣称可使用写/测试工具 | 可信注册表拒绝；不启动工具 |
| D3 | 独立/依赖只读节点 | 最多两路并行，依赖仍按 succeeded 等待，预算正确 |
| D4 | 多个 edit/verify 节点 | 主 Agent 串行；无写读冲突 |
| D5 | 工具成功但条件证据缺失 | 节点不成功；下游不得 ready |
| D6 | 上游失败、不确定 | 后继 blocked，原因和证据保留，无死循环 |
| D7 | 写后文件变化、外部修改、Observation 过期 | 相关派生证据失效；旧 edit 不自动重放 |
| D8 | 静止点重规划、非法候选、达到次数上限 | 旧版可查，原子提交或保留，诊断明确 |
| D9 | 派发前/刚派发/运行中 crash | journal 可判阶段；旧执行核查后才续跑 |
| D10 | 结果 durable、reducer 前或 checkpoint 前 crash | 接纳一次可信结果，避免重复工具执行 |
| D11 | 修改部分写、收据缺失/损坏、迟到结果 | uncertain、diff 与阻塞正确，不盲目重跑 |
| D12 | 并行只读中一项取消/失联 | 只重启安全项，不误收旧 attempt 结果 |
| D13 | 验证进程中断、测试收据完整/不完整 | 分别接纳或新 attempt 重测；不误报通过 |
| D14 | workspace/task/schema/checksum 不匹配 | 拒绝自动恢复，保留诊断 |
| D15 | 真实 L2 修复任务 | 证据驱动 Plan、串行补丁、验证和恢复入口贯通 |

故障注入要在每个持久化边界触发，不能只在函数调用前后 mock 一个异常。进程级中断使用专用临时仓库，记录旧进程是否停止、文件后态、journal/checkpoint和实际工具调用次数。D15 至少一次正常修复和一次已知中断后恢复，不用历史或假结果冒充。

没有 Todo/DAG 性能或修复率对照实验。保留验收证据、测试记录、版本与已知限制；不报告未测量的提效幅度。

## 10. 交付与纪律

配套 plan：`docs/superpowers/plans/2026-09-30-plan-dag-inflight-resume-mvp.md`。开发前按阶段说明涉及模块和预计变更，保护当前未提交修改。先跑相关测试和 lint/format；全量测试仅在用户显式授权时运行。真实模型调用或远端操作按会话授权范围执行。

参考：[OpenCode Plan/Explore 官方文档](https://opencode.ai/docs/agents/)、[Pi plan-mode 示例](https://github.com/earendil-works/pi/tree/main/packages/coding-agent/examples/extensions/plan-mode)。参考的是只读探索、进度与持久化方式；本文的任务 DAG 与在途恢复是 FixLoop 自己的设计。
