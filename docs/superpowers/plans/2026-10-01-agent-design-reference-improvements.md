# Agent 设计参照：剩余实施计划

日期：2026-10-01，增量更新至 2026-10-02。依据：[五维对照与技术取舍](../../AGENT_DESIGN_REFERENCES_2026-10-01.md)。状态：P0 静态对账及本页列出的 Plan、代码探索消费链、长任务状态与上下文、上下文组装、证据消费、决策与恢复投影、内核职责拆分、工具批次预检、恢复与取消、Subagent 一致性选定 MVP 已完成相关验收。P1–P4 的其余内容仍是待逐项讨论的候选，不能据此宣称已整体实施。

## 顺序与范围

当前顺序为 P0 → Plan MVP → 后续领域逐项确认。下文 P1–P4 保留为候选拆解，后续需按代码探索、长任务状态、工具批次、恢复/取消和 Subagent 的 MVP 讨论结果收敛，不表示这些领域已取消或最终范围已确定。沿用 Python、Layer 1/Layer 2 和既有持久层；不引入兼容层、第二套任务状态机、数据库服务或新的重试控制器。

旧 MVP 文档的工期/行数是初始范围估计，不能叠加为当前剩余工期。实施前按实际差量重新估算并确认；以下模块是接入位置建议，不要求为每个接口新增文件。保护现有未跟踪内容；不自动 push、创建 PR 或运行全量测试。

## P0：规格与实现对账（本轮文档已完成）

记录当前 commit、权威状态来源、源码缺口、已有验收及后端限制。Plan/Batch/Explorer 已有实现，coordination 已有验证范围；long-task 只是部分实现，C1–C9 不能整体标为通过。对应 spec/plan 增加本次对账入口，保留原有设计契约。

门禁：读者能区分源码已有、历史验收、本次静态发现和未来保证。本次只检查文档链接、diff 与格式，不重新验证生产行为。

## Plan MVP：统一视图与重规划决策（已实现，相关验收通过）

规格与 P1–P7 验收见 [Plan MVP](../specs/2026-10-01-plan-view-replan-decision-mvp.md)。只交付两项：

1. PlanSession 从同一已校验快照生成只读计划视图，接入当前节点上下文、工具准备和 plan_progress；节点状态继续由 reducer 修改。
2. 小型决策入口只接通已确认的代码验证失败，区分保留计划、重取证据、重规划、阻断和停止。复用失败分类、Orchestrator retry/rollback、两次重规划上限及安全提交；候选输入增加旧计划、真实失败收据/反馈和新鲜源码证据。

先做投影接入，再做决策与输入增强，最后补已有真实 L2 失败→回滚→重规划测试。重复触发关联现有 journal 并幂等处理，环境失败不误触发，uncertain 不生成候选。其余触发、内核拆分和新持久层不进入本 MVP。

已交付 `PlanSession.plan_view()`、纯 `decide_replan()`、增强候选输入和 journal 触发关联。默认控制台及现有 JSONL 输出实时阶段；未对账的中断请求保持 recovery_required。相关回归 113 项通过，另补提交/分类/预算场景；记录区分真实 host pytest 与受控 provider，不宣称线上修复率提升或全量通过。

## 代码探索消费链 MVP（已实现，相关验收通过）

本轮确认的增量为 RetrievalResult 贯通 Observation/模型消费，以及消费时有界复验与显式失效。复用已有检索、Python LSP、任务局部关系与 context manifest；不增加全仓图、探索调度器、自动重取或新的重规划触发。相关节点去重后 164 passed、1 skipped，实际 XML/native 请求、恢复续接、L2 读→改→host pytest 和技术取舍见 [验收记录](../../CODE_EXPLORATION_CONSUMPTION_ACCEPTANCE_2026-10-01.md)。下述 P1–P4 其余内容仍为候选范围。

## 长任务状态与上下文 MVP（已确认并实现）

本轮收敛为统一权威任务/Plan 投影、必需预算门禁、恢复后的当前上下文重建。复用已有模块，普通调用示例不常驻 Plan 请求，完整角色规则保留，默认预算不增加。真实 XML/native 请求、压力→修改→恢复→host pytest、零请求阻断及技术取舍见 [验收记录](../../LONG_TASK_CONTEXT_ACCEPTANCE_2026-10-01.md)。当时后置的显式决策版本已由下述增量落实；自动提炼、长原文语义摘要和全部入口迁移仍后置。P1/P2 原目标不整体标为完成。

## 上下文组装 MVP（已确认并实现）

在长任务 MVP 上增加 XML/native 统一 `prepare_request()` 入口、显式协议参数和受保护恢复指令；Plan 可选段采用软配额、回收池与节点阶段优先级，输入总上限不增加。源码片段/工具批次按完整单位选择，编码后计量并生成最终 manifest，发送前复验。范围与技术取舍见 [验收记录](../../CONTEXT_ASSEMBLY_ACCEPTANCE_2026-10-02.md)。普通 L1 固定配额及既有恢复契约保持；全局排序、更多自动摘要/检索、全部入口迁移和其余内核拆分仍后置。

## 证据消费 MVP（已确认并实现）

复用 EvidenceLedger/Observation，增加结构化校验解释与当前/历史用途；四类持久事实进入当前节点必需摘要，正文继续使用可选预算和既有 freshness 检查。最终消费 manifest 区分校验、摘要和正文入选，按引用及用途记录，checkpoint 深拷贝。显式 native 工具尾部也检查对应引用，完整记录校验与确认后态恢复保持。实际 XML/native、L2 修复与进程恢复及技术取舍见 [验收记录](../../EVIDENCE_CONSUMPTION_ACCEPTANCE_2026-10-02.md)。总预算不提高；自动提炼、重取、重规划和全部消费者迁移继续后置。

## 决策与恢复投影 MVP（已确认并实现）

owner 显式创建/替换决策，版本追加并绑定当前节点、Plan version、证据引用与完整 checksum；当前节点只消费有效最新版，失效派生 needs_review，其他节点不受无关旧决策阻断。决策证据接入既有必需摘要与消费记录，来源复核/证据替换保留为审计事实。checkpoint 深拷贝，seal 的任务快照核对 journal 前缀，合法进度后从当前 journal 重建。确认后态不因旧决策过期重放；范围、实际 XML/native、进程中断恢复及技术取舍见 [验收记录](../../DECISION_PROJECTION_ACCEPTANCE_2026-10-02.md)。来源 turn 注册、任务级作用域、自动提炼与全入口迁移继续后置。

## 工具批次预检 MVP（已确认并实现）

native 批次建立时冻结嵌套 schema，全部调用先做纯参数预检；单项错误保留结果配对和 Observation，不创建执行 Action/Plan operation 或预约调用预算。原始 content 可用时检查数量/次序、身份与参数，结构或协议错误整批零执行；provider 保留非法 input，既有仅规范化调用 adapter 契约保留。截断回合丢弃沿用已有行为，Executor 继续执行最终门禁。相关节点去重 119 passed，新增 20 个，借鉴与技术取舍见 [验收记录](../../BATCH_PREFLIGHT_ACCEPTANCE_2026-10-02.md)。不增加批次事务/回滚、DAG、XML 批次、白名单扩展或重试控制器。

## 恢复与取消 MVP（已确认并实现）

新执行 run_id 与严格 resume_run_id 分开；恢复入口检查 checkpoint 完整性、schema、身份、任务原文及消费字段，失败不进入 owner/模型/工具。公开 repair、进度和报告消费当前协调/Plan 生成的结果投影，取消请求、清理确认及最终取消分别展示。失败清理后的同 generation 重复取消只返回持久结果；重新尝试需显式恢复，旧 generation 不获得权限。相关节点去重 102 passed，新增 21 个，实际进程恢复、host pytest 和取舍见 [验收记录](../../RECOVERY_CANCEL_ACCEPTANCE_2026-10-02.md)。复用现有恢复器，不重建状态机；自动重试、远程恢复、新后端及其余入口迁移后置。

## Subagent 一致性 MVP（已确认并实现）

在既有只读 Explorer 上增加同源 Plan/node 委派快照、native 原始协议/schema 预检和 collect 的校验/执行/清理诊断。快照完整保留目标与硬约束，超过 8000 UTF-8 字节在任务创建/预留前拒绝；恢复保留原快照、继续依赖既有 attempt/generation 与保守收据结算。相关节点去重 69 passed，新增 26 个；真实 XML/native L2 委派→owner 重读→单次写→host pytest，以及进程退出恢复见 [验收记录](../../SUBAGENT_CONSISTENCY_ACCEPTANCE_2026-10-02.md)。不增加角色、递归、子工具并行、自动重试或语义接纳，Hermes 补充继续后置。

## P1：必需上下文与证据门禁（选定 MVP 已覆盖，完整候选范围保留）

模块：`agent_runtime/context_manager.py`、`section_filler.py`、`context_runtime.py`、`message_projection.py`、必要的 AgentLoop 接入。

1. 一次组装权威 state，消除重复 state 键的覆盖和 used 累加；普通 L1 与带 Plan 的 L2 明确选择状态来源。
2. 先核算受信任执行规则、目标、全部适用约束、当前节点/完成条件和必要证据，并预留 provider 协议与输出空间；可选知识/历史只消费剩余预算。工具调用/结果按完整组投影，不能留下不配对消息。
3. Plan 状态构建失败返回稳定错误；必需项超预算、缺失或身份冲突时，不调用模型。超长原始请求使用可追溯投影，无法完整表达约束则阻断。
4. 展开 Observation 前复验依赖文件 hash、scope、blob、完整性；返回 freshness/partial/truncation 信息。复用已有 RetrievalResult，不把片段新鲜等同于搜索完整。
5. selection manifest 记录所需/选中/舍弃引用、原因、预算与实际输出 hash；native/XML 共用语义门禁。

相关验证：补充 `tests/test_context_manager.py`、`test_context_projection.py`、`test_context_runtime_governance.py`、`test_long_task_context.py`，按改动选择 AgentLoop/native 用例。新增场景只在相关测试文件中增加，不镜像实现。

门禁：低预算、错误 Plan 状态、外部文件变化和截断结果均不能绕过模型调用/证据门禁；实际文本与预算计量一致。

## P2：决策版本与恢复后的状态投影（选定 MVP 已覆盖，完整候选范围保留）

模块：`agent_runtime/plan_runtime/long_task.py`、`session.py`、`journal.py`、`checkpoint.py`、`src/repair/plan_binding.py`。

1. 扩展现有 LongTaskState，记录原始请求来源与 checksum、约束来源 turn、Plan identity/revision 引用；Plan 节点仍由 reducer 独占修改。
2. 增加 active/superseded 决策记录、evidence refs 和 supersedes 链；旧证据使相关决策 needs_review，子任务结果不能直接成为权威决策。
3. 上下文投影不暗中修改持久状态；freshness 更新如需落盘，由显式 owner 操作递增 revision 并提交 journal。
4. 在既有 seal 中封装活动决策和证据引用；保持 owner/reconcile 在上下文恢复之前。不同阶段 seal 与 journal 的合法前滚由现有恢复器判断。
5. 摘要附覆盖范围和来源；恢复使用当前节点与已核验证据，失败明确区分 state_mismatch、needs_retrieval 与 action uncertain。

相关验证：长任务、Plan runtime/crash、checkpoint 与公开 repair resume 的直接相关用例；对既有收据崩溃窗口只在改动影响时复跑。

门禁：错误摘要无法改写约束；过期决策不作为当前事实；checkpoint/journal 身份冲突拒绝，已发生 edit 不重复执行。

## P3：按 Pi 边界拆分内核（选定 MVP 已覆盖，完整候选范围保留）

本轮已完成工具批次编排和 Observation 记录拆分：以明确依赖及 owner 回调连接准备/执行/归并，XML/native 共用记录服务；现有 Scheduler/Executor/Plan 收据与读许可继续复用。新增模块无 src 依赖，Loop 的修复策略与 edit-lock/grounding 依赖尚未迁移；完整 P3 继续保留。范围、模块独立行为、真实 host pytest/进程中断及技术取舍见 [验收记录](../../KERNEL_SPLIT_ACCEPTANCE_2026-10-02.md)。

模块：`agent_runtime/agent_loop.py`、`context_manager.py`、`tool_batch.py`、`tool_executor.py`、`src/repair/`。

1. 请求准备已落实本轮选定 MVP：权威状态投影/选择与 provider 消息转换分离，native/XML 主循环共享入口、必需门禁和最终 manifest；全部其他入口迁移保留为后续范围。
2. 已抽出批次准备/执行/归并接线和统一 Observation 记录，保留可信注册表、九道 Executor 闸口、独立 ToolContext 和 owner 提交；工具步骤其余策略迁移按后续需求收敛。
3. 将修复专属预算/收敛/patch recovery 策略逐步移至 L2；L1 仅消费显式策略接口，不导入 `src`。
4. 事件回调只消费已提交事实，不能发明 Plan 成功；trace/TurnProgress 继续作为投影。
5. 已完成选定工具批次 MVP：派发前纯 schema 预检和 native 原始调用完整性核对；截断回合零派发沿用既有实现并验证。更广预检、XML 批次、事务或白名单扩展继续后置，见上文验收。

相关验证：受影响 AgentLoop、native Batch、ToolExecutor、Plan integration 和 repair binding 用例。没有行为需求变化时不重写整个循环或新增双轨兼容模式。

门禁：原序 call/result 配对、共享只读峰值 2、预算、取消与 uncertain 契约保持；L1 无 `src` 依赖。

## P4：Hermes 专项补充——历史检索与经验候选

模块：现有 `agent_runtime/session_store.py`、`features/memory/` 和已有 Skill 路由；检索接口归属 L1，修复经验提炼规则归属 L2。

1. 核查当前会话存储与 SQLite FTS5 可用性；优先索引已有授权历史，索引可重建，原会话仍为来源。FTS5 不可用时明确报告能力缺失，不新增替代检索后端。
2. 同 workspace、授权 session 的 history search 返回稳定消息/来源引用、范围和限额；匹配文字不能被解释为受信任指令。
3. 详情按需加载，遵守敏感路径、脱敏和 token 上限；历史命中仅作线索，当前源码必须重新核验。
4. 从有验证收据与 patch 来源的任务产生经验候选，保存适用范围、证据、版本与失效条件；通过已有审核流程后才能成为 active 记忆/Skill。
5. 默认不跨任务复用当前代码事实，不自动发布 Skill，不记录评测答案或 Case 专属规则。

相关验证：同词跨 workspace 拒绝、删除后索引重建、详细输出裁剪、历史文本提示注入、经验撤销/失效，以及来源缺失时拒绝激活。

门禁：检索授权与来源可核验；历史无法直接满足 Plan completion 或授予写入权限；经验只是可撤销的通用候选。

## 交付标准

每阶段单独记录实际实现、相关测试、受影响文件 lint/format、差异与限制。P1/P2 必须有至少一条受控 provider 驱动的真实 L2 工具/磁盘/测试流程，经历上下文压力、文件修改和中途恢复；与在线模型效果分开报告。

不以文件拆分数量、代码行数、产品功能数量作为成功标准。报告实际需求保留、过期证据拒绝、重取、调用/token、重复副作用与耗时。阶段未通过门禁时只报告已完成范围，不能整体标为已实现。
