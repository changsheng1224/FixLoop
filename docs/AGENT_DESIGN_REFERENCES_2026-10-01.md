# Agent 设计参照与 FixLoop 技术取舍

日期：2026-10-01，增量更新至 2026-10-02。状态：设计方向已获用户确认；Plan、代码探索消费链、长任务状态与上下文、上下文组装的选定 MVP 已实现并通过相关验收；入口与剩余范围见 [实施计划](superpowers/plans/2026-10-01-agent-design-reference-improvements.md)。其余候选继续逐项讨论，源码现状表保留初次静态审计基线。

本地审计基线：`7d04bb68604358e780bc80afc56819502868b2ee`。以 OpenCode 为主要参照、Pi 为运行内核参照、Hermes Agent 为专项补充，Codex 与 Claude Code 用于交叉检查。本文的优先级、契约和实施顺序是 FixLoop 的设计判断，不能归属为上游已经提供的保证。

上游资料于上述日期实际读取；文档页面和 GitHub `main/dev` 链接是动态来源，未固定上游 commit，也未运行上游产品。比较的是所查资料明确描述的能力，不做整体性能排名。初次静态审计与后续 Plan MVP 的相关回归分别记录，不能推断所有历史验收或后端均重新验证。

## 1. 结论与架构边界

采用 OpenCode 的角色、工具权限、按需探索和会话事件组织；采用 Pi 的循环、上下文变换、模型消息转换、工具执行与事件边界；采用 Hermes 的历史按需检索和经验渐进加载。保留 FixLoop 的证据驱动 DAG、执行收据、owner/generation 门禁和未知副作用阻断。

Layer 1 提供通用运行机制：模型调用、消息投影、工具闸口、预算、Observation、Plan reducer/journal、恢复协调。Layer 2 提供修复任务的角色、完成条件装配、重试与最终验证。Layer 1 不导入 `src`。Pi 是设计参照，继续使用现有 Python 实现，不引入 TypeScript 内核、LLM 框架或跨语言桥接。

计划、证据、历史和执行事实各有权威来源：

| 信息 | 权威来源 | 允许的消费方式 |
|---|---|---|
| 节点依赖、完成条件、版本与终态 | PlanSession / reducer / journal | 当前节点投影；Todo 和 UI 只读派生 |
| 原始请求、用户约束、关键决策 | 现有 LongTaskState 的后续版本 | 来源与 revision 校验后渲染 |
| 当前代码事实 | Observation / EvidenceLedger / 文件版本 | 校验作用域、完整性和 freshness |
| 在途执行、写入是否完成 | durable operation/attempt 收据与工作区后态 | 恢复对账；未知写入保持 uncertain |
| 会话摘要、历史经验、Subagent 发现 | 历史记录、记忆库、候选结果 | 待核验线索；不能直接推进完成条件 |
| 执行权、取消与资源清理 | RunCoordinator / 资源收据 | generation 门禁；清理确认后提交终态 |

## 2. 本地现状与实际缺口

下表的“已有”指源码或已有验收记录，不表示每个旧 spec 条款均已完成。

| 能力 | 已有基础与证据 | 本轮识别的剩余问题 |
|---|---|---|
| Plan DAG | [实现说明](PLAN_DAG.md)、[Python/host 验收](PLAN_DAG_ACCEPTANCE_2026-09-30.md)；[PlanSession](../agent_runtime/plan_runtime/session.py) | L1 Todo 与 L2 Plan 展示需明确区分；禁止 Todo 工具成功即推进的语义进入 repair DAG |
| 代码探索 | [验收记录](superpowers/evidence/code-exploration-p4/acceptance.md)、[RetrievalResult](../agent_runtime/code_exploration/models.py) | 已有范围、partial、版本和 LSP 降级字段；需贯穿上下文展开与决策消费，避免重复发明检索协议 |
| 长任务状态 | [LongTaskState / LongTaskContext](../agent_runtime/plan_runtime/long_task.py)、[已有测试](../tests/test_long_task_context.py) | 已有持久状态、当前节点和 evidence refs；决策缺少显式 active/superseded、证据依赖与用户来源链 |
| 上下文预算 | [ContextManager](../agent_runtime/context_manager.py)、[SectionFiller](../agent_runtime/section_filler.py) | 长任务状态在可选段之后按预算裁剪；构建 ValueError/OSError 时可返回空串；缺少必需状态的失败门禁 |
| 状态段组装 | 同上 `_fill_sections` / `add_section` | 两次使用 `state` 键，第二次非空会替换内容而 used 累加；需要单次权威状态组装及一致计量 |
| 证据消费 | [EvidenceLedger.valid](../agent_runtime/plan_runtime/evidence.py)、[ObservationStore](../agent_runtime/context_runtime.py) | Plan 校验已有文件版本；`expand_for_context` 检查 stale 标志与 blob，但入口未直接复验当前依赖文件，也未返回明确裁剪标记 |
| 工具批次 | [native 批次交付](TOOL_BATCH_TURN_PROGRESS.md) | 已有 call 身份、原序归并、共享两路只读 permit；后续内核拆分必须保存这些契约 |
| 恢复协调 | [使用与验证范围](RUN_COORDINATION.md)、[run_coordination](../agent_runtime/run_coordination/coordinator.py) | 已有 run owner、generation、资源登记和清理门禁；不是待新建能力 |
| 只读 Subagent | [验收记录](superpowers/plans/2026-10-01-subagent-readonly-acceptance.md) | 已有独立循环、稳定句柄、预算和 freshness；验收模型为受控客户端，不是在线模型效果证明 |
| 历史记忆 | [CanonicalMemoryStore](../agent_runtime/features/memory/store.py)、[会话存储](../agent_runtime/session_store.py) | 已有记忆治理和持久化；不能因此宣称已有 Hermes 式历史会话 FTS 检索 |

状态段覆盖及展开入口的发现来自静态源码，尚未新增回归测试。本轮记录为后续修正项，不声称已经修复。WSL 后端另有专用验证记录，但 [repair_factory](../src/repair_factory.py) 仍包含 `wsl_bwrap` repair 门禁，不能因协调模块已有实现而宣称该 CLI profile 已开放。

## 3. 计划：区分权限模式、提示计划与执行 DAG

| 参照 | 所查设计 | 对 FixLoop 的启示 |
|---|---|---|
| OpenCode | Build/Plan primary agent；Plan 默认对 edit/bash 设为 ask；Explore 是只读 subagent | 模式用权限落实；Plan 名称本身不等于绝对禁止副作用 |
| Pi | 通用循环与扩展边界，工作流由上层组织 | 把修复规划放在任务策略；不把通用 loop 变成第二个修复控制器 |
| Hermes | 专项参照聚焦记忆和 Skills，本文不推断其提供 FixLoop 式 DAG | 经验可影响探索问题，不能成为计划完成事实 |
| Codex / Claude Code | Codex 支持会话继续；Claude Code Plan 模式用于探索和提出方案 | 人可读计划、权限模式与 durable DAG 分开评价 |

来源：[OpenCode Agents](https://opencode.ai/docs/agents)、[Pi Agent Core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)、[Claude Code 工作方式](https://code.claude.com/docs/en/how-claude-code-works)。

决定：沿用最多 8 节点、两次重规划的小图，由模型提出候选，校验器检查依赖和可信能力，reducer 依据收据和证据推进。重规划继续只在静止点提交。L2 Todo 从当前 DAG 派生；普通 L1 Todo 的独立适用范围必须明确。分析结论记录只能证明过程发生，不能证明语义正确。

取舍：不引入独立 Planner Agent、通用工作流引擎或任意运行中改图。小图减少调度复杂度，代价是大型任务需要分段。验证失败重试仍由 Orchestrator 决定，Plan 不再重试同一个补丁。

本轮用户进一步确认的 [Plan MVP](superpowers/specs/2026-10-01-plan-view-replan-decision-mvp.md)只补两项：统一供上下文/工具准备/进度消费的只读计划视图，以及小型重规划决策入口。入口首版仅接已确认代码验证失败，复用既有 retry/rollback/止损/门禁，增强旧计划与失败反馈输入；环境失败、未知执行和预算不足分别处理。其余领域仍逐项讨论，不因本轮聚焦 Plan 而取消。

## 4. 检索：逐步缩小范围与统一证据消费

| 参照 | 所查设计 | 对 FixLoop 的启示 |
|---|---|---|
| OpenCode | 文件/文本工具、Explore、实验性 LSP 定义/引用等操作 | 优先按需定位；实验工具支持不能等同于完整语义图 |
| Pi | 外部上下文可在 transformContext 接入 | 检索作为服务消费，不在循环内部写各语言索引规则 |
| Hermes | 会话存于 SQLite，支持 FTS5 历史检索及原消息访问 | 历史按需取回，减少常驻历史摘要负担 |
| Codex / Claude Code | 工具环境与独立子上下文支持调查；Claude Explore 有独立工具范围 | 聚焦问题委派后回收来源，避免复制整段主对话 |

来源：[OpenCode Tools](https://opencode.ai/docs/tools/)、[Pi Agent Core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[Hermes Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/)、[Claude Subagents](https://code.claude.com/docs/en/sub-agents)。

决定：默认路径为路径/文本搜索 → 受限源码读取 → 按需定义/引用 → 收集候选关系 → 主 Agent 核验。选择下一步由模型根据当前证据决定，不硬编码特定文件或错误文本。复用已有 RetrievalResult，贯穿 `execution`、`completeness`、`scanned_scope`、`truncation_reasons` 和 `dependency_versions`。

freshness 与完整性是两个轴：返回片段的文件版本吻合，只说明片段新鲜；不说明搜索完整。空结果、截断结果、候选 AST/LSP 边不能提升为“仓库不存在该符号”或“所有测试已覆盖”。范围版本不足时负向结论保持 unknown。

取舍：首期保留 Python LSP 和任务局部关系视图。暂缓全仓图数据库、向量服务和目录监听。历史检索先限定同一 workspace 的授权会话，检索命中返回来源引用；重新读取源码后才允许生成当前 Plan evidence。

2026-10-01 增量已实现检索语义贯通、消费时有界复验与明确失效原因，实际 XML/native 请求、恢复续接和 L2 真实修复链路见 [消费链 MVP 验收](CODE_EXPLORATION_CONSUMPTION_ACCEPTANCE_2026-10-01.md)。unknown 不提升为当前源码；模型按普通工具重取，代码探索不自动触发重规划。此记录不代表历史检索、全仓索引或通用上下文管线已整体实施。

## 5. 上下文：权威状态先入预算，历史按需投影

| 参照 | 所查设计 | 对 FixLoop 的启示 |
|---|---|---|
| OpenCode | 配置区分 auto compaction、prune 和 reserved；源码单独处理旧工具输出 | 分开处理输出裁剪、历史摘要和窗口预留；不复制其 token 常量 |
| Pi | transformContext 后再 convertToLlm；compaction 记录摘要和保留边界 | 语义投影和 provider 编码分离；摘要覆盖范围应可追溯 |
| Hermes | 小容量常驻记忆，历史按需查找；Skills 渐进加载 | 高频稳定事实与详细历史使用不同预算和加载路径 |
| Codex / Claude Code | Claude 主对话与 Subagent 上下文有边界，长期工作需上下文管理 | 子任务传目标和最小必要证据；摘要不得替代用户硬约束 |

来源：[OpenCode Config](https://opencode.ai/docs/config/)、[Compaction 源码](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts)、[Pi Compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)、[Hermes Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/)、[Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)、[Claude Code 工作方式](https://code.claude.com/docs/en/how-claude-code-works)。

决定：扩展现有 LongTaskState；Plan 引用只有一份来源。将上下文投影收敛为同一条调用前管线，native/XML 共享语义层，仅在最后转换 provider 格式：

```text
可信任务/Plan revision
  → 校验目标、约束、当前节点、决策来源
  → 核验必需证据的作用域、版本与完整性
  → 先核算必需内容与输出/工具协议预留
  → 选择可选代码、近期对话、历史摘要
  → provider 消息转换与最终窗口校验
  → 写最终 selection manifest，发送前复验
```

必需内容包括受信任执行规则、目标、约束、当前节点、必要证据及活动工具协议；无法完整表达、Plan 引用不符或必要证据不可用时，返回结构化阻断并且不调用模型。原始请求过长时保存完整引用和 checksum，只采用可追溯投影；禁止用头尾裁剪声称已保留全部硬约束。状态段只组装一次，计量与实际消息内容一致；provider 的工具调用/结果配对按完整组保留，不能裁掉半组后继续请求。

DecisionRecord 的目标字段为 `decision_id/revision/status/statement/rationale/evidence_refs/source_turn_id/supersedes_id`。只向上下文投影 active 决策；证据过期标 needs_review，保留历史版。子任务发现不直接写权威决策。压缩摘要记录覆盖范围、引用和生成版本，只充当历史线索。

取舍：继续复用 L0–L5、Observation blob、ContextPolicyEngine 和现有记忆治理，不新建平行上下文数据库。稳定 system/tool 前缀继续复用，Plan/文件版本等动态状态不放入冻结前缀。强门禁可能增加重取或缩小任务的次数，换取明确可诊断的失败。

2026-10-02 已落实本轮确认的长任务状态/上下文 MVP：单次权威投影、完整必需项先占预算、XML/native 共享语义门禁、恢复后消费当前 Plan。通用调用示例不常驻 Plan 请求，角色规则完整保留，默认预算不增加。超长原文暂时阻断；当时后置的显式决策版本已由下述增量落实，自动提炼和全部入口迁移仍后置，见 [验收与取舍](LONG_TASK_CONTEXT_ACCEPTANCE_2026-10-01.md)。本节其他目标仍为后续方向。

同日完成已确认的 [上下文组装 MVP](CONTEXT_ASSEMBLY_ACCEPTANCE_2026-10-02.md)：XML/native 主循环共享请求准备入口，显式传入 schema/工具尾部/恢复指令，Plan 可选内容用软配额和回收池按阶段借额度；完整工具组选择、编码后计量和最终请求 manifest 已接通。总预算不扩大，不新增全局排序、摘要决策或全部入口迁移。

此前上下文方向又完成 [证据消费 MVP](EVIDENCE_CONSUMPTION_ACCEPTANCE_2026-10-02.md)：现有谓词返回类型/状态/原因与当前或历史用途，四类持久事实进入当前节点必需摘要，最终 manifest 区分校验、摘要与正文入选。完整校验保留于 ledger/manifest，模型摘要仅使用短 checksum 显示标识；历史依赖不触发写入重放。该投影格式为 FixLoop 本地设计，不新增自动提炼、检索或重规划。

2026-10-02 又完成 [决策与恢复投影 MVP](DECISION_PROJECTION_ACCEPTANCE_2026-10-02.md)：owner 显式决策按版本追加，绑定节点、Plan version 和证据记录 checksum；当前节点消费有效最新版，失效只读派生 needs_review。来源复核/证据替换不提升为策略，恢复使用当前 journal 重建，checkpoint 深拷贝并核验任务快照与 journal 前缀。首版 source 为审计引用，完整 source_turn 注册、自动提炼、任务级作用域和全入口迁移后置。

## 6. 工具：小内核与受控执行边界

| 参照 | 所查设计 | 对 FixLoop 的启示 |
|---|---|---|
| OpenCode | allow/ask/deny 权限；工具与角色装配；会话服务与事件接口 | 能力配置统一进入可信执行层，展示层消费事件 |
| Pi | 当前 Agent Core 支持批次并行/串行配置、before/after hook；完成事件与结果原序不同 | 预检、执行、归并分开；不可把 hook 或 UI 完成事件当作权限证明 |
| Hermes | Skills 按需加载、可沉淀流程知识 | Skill 影响任务上下文，不能改变工具副作用分类或放宽权限 |
| Codex / Claude Code | Codex 将审批时机与沙箱资源边界区分；Claude 子 Agent 可限制工具 | 角色授权、执行审批与 OS 隔离分别承担责任 |

来源：[OpenCode Permissions](https://opencode.ai/docs/permissions/)、[Server](https://opencode.ai/docs/server/)、[Pi Agent Core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[Hermes Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)、[Codex Sandbox](https://learn.chatgpt.com/docs/sandboxing)、[Claude Subagents](https://code.claude.com/docs/en/sub-agents)。

决定：先稳定行为契约，再逐步抽离 AgentLoop 的上下文准备、工具批次执行和 L2 修复策略。最小接口职责为请求投影、工具预检/执行/归并、收据持久化和终态检查；不是可任意覆盖闸口的插件系统。

沿用 call/turn/batch/attempt 身份，worker 返回结果、主 owner 归并；模型结果按 ordinal 与 provider call ID 配对。Plan、native Batch、Explorer 共用已有两路只读 permit 和父预算。写入、命令、测试保持可信分类与现有串行门禁；工具名或模型参数不能自报只读。

Subagent 继续仅支持实现定位与相关测试发现，固定上限两项、独立上下文和只读 allowlist。收集稳定句柄，重复 collect 幂等；完成后主 Agent 重读来源再作决策。暂缓递归委派、多个写 Agent、任意 Subagent DAG。

取舍：不复制 Pi 当前默认并行策略，也不照搬 OpenCode 默认权限配置；保留 FixLoop 的已审计白名单。暂缓新的 HTTP server/SSE 服务，先复用 canonical trace 与 TurnProgress，避免引入与执行 journal 竞争的事实源。

2026-10-02 已完成选定的 [内核职责拆分 MVP](KERNEL_SPLIT_ACCEPTANCE_2026-10-02.md)：工具批次编排与 XML/native 共用的 Observation 记录从主循环抽出，以明确依赖和 owner 回调接线，复用现有调度器、Executor、Plan 操作和读许可。新增模块无 src 依赖；现有 Loop 的修复策略与 edit-lock/grounding 依赖仍保留，完整 P3 不标为完成。原序结果引用、取消/超时、真实进程退出和 host pytest 已做相关验证。

同日完成 [工具批次预检 MVP](BATCH_PREFLIGHT_ACCEPTANCE_2026-10-02.md)：冻结 schema，派发前检查全部调用；参数错误单项配对拒绝，合法兄弟继续，拒绝不创建执行 Action/Plan operation 或预约调用预算。可用原始 content 与调用数量/次序、ID、名称及输入不一致时整批阻断；provider 保留非法 input。Executor 继续负责规范化与权限，已有截断回合丢弃行为补充验证。无事务/回滚、白名单扩展、XML 批次或新重试控制器。

## 7. 恢复：分别恢复对话、上下文与执行事实

| 层次 | 参照与差异 | FixLoop 决定 |
|---|---|---|
| 对话继续 | Codex resume、OpenCode 会话接口、Claude 可恢复的子 Agent | 会话可访问历史，不代表旧进程停止或写操作可以重放 |
| 上下文重建 | Pi 摘要/保留边界，Hermes 历史检索 | 按当前节点重组，历史命中和摘要均不充当执行收据 |
| 在途恢复 | 上述文档不足以证明 FixLoop 所需的副作用保障 | 继续依赖现有 Plan journal、精确收据、工作区后态及停止证明 |
| 取消终态 | 通用 abort/工具事件不能证明底层资源退出 | 子资源清理未确认时保持 recovery_required，拒绝后续冲突写入 |

来源：[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)、[OpenCode Server](https://opencode.ai/docs/server/)、[Claude Subagents](https://code.claude.com/docs/en/sub-agents)、[Pi Compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)、[Hermes Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/)。未证明上游提供某项保障，不等于断言上游没有实现。

唯一恢复次序：取得 owner/generation → reconcile 旧资源 → 对账 Plan attempt/operation 收据 → 校验 workspace 后态 → 核验任务/决策/活动证据 → 重建当前节点上下文 → 满足门禁后派发。

checkpoint 是 journal/活动引用的封装，不是新的执行事实来源；TurnProgress 只用于显示。活动证据和收据不能受“最近 100 条”窗口淘汰。恢复后证据变旧只要求重取或复核；已经发生的 edit 不因输入证据过期而再执行。无法确认副作用则 uncertain，不承诺 exactly-once 或跨宿主机互斥。

2026-10-02 完成选定的 [恢复与取消 MVP](RECOVERY_CANCEL_ACCEPTANCE_2026-10-02.md)：新 run ID 与严格恢复意图分开；无效 checkpoint 在 owner/模型/工具之前阻断。从当前协调存储与 Plan 恢复报告生成统一诊断，公开状态、进度与报告共享；取消请求、清理预备状态和最终取消分开，清理不提升为写入核验。失败清理后的同 generation 重复取消只返回已有结果，接管后旧 generation 仍拒绝。复用原有恢复器与回滚 fence，不增加自动重试或第二份执行账本。

## 8. 借鉴记录与技术取舍

| ID | 借鉴来源 | 采用内容 | 舍弃/延后及代价 | 落地位置与状态 |
|---|---|---|---|---|
| R-01 | OpenCode Plan/Build/Explore | 角色、权限和聚焦探索边界 | 不以模式名称替代 DAG 完成判断；保留小图约束 | Plan/Explorer 已有；Todo 投影一致性待核查 |
| R-02 | Pi transformContext/convertToLlm | 统一语义投影后转换 provider 消息 | 不引入 TS 内核；逐步拆分仍需维护 Python 契约 | XML/native 请求与 Observation 记录、决策投影与最终 manifest 已实现；其余入口和策略迁移后置 |
| R-03 | OpenCode prune/compaction、Pi compaction 边界 | 输出外置、历史覆盖范围和摘要可追溯 | 不复制 token 常量；增加 manifest 成本 | 复用 Observation/L0–L5；Plan 必需门禁及可选段弹性预算已实现，算法属 FixLoop 设计 |
| R-04 | OpenCode 工具与实验 LSP | 路径/文本优先、定义/引用按需 | 不做全仓图和自动多跳；负向结论保守 | code_exploration 已有，消费链 MVP 已验收 |
| R-05 | Pi preflight/执行/结果次序 | 工具调用生命周期与原序归并 | 不放开未经审计工具并行；无批次事务/回滚 | 已抽出批次编排；native 纯 schema 预检与原始协议核对已验收，复用 Scheduler 与原序结果引用 |
| R-06 | OpenCode Permissions、Codex Sandbox | 角色授权/审批/OS 隔离分层 | 不扩大默认权限；WSL 产品门禁保持 | Executor/Gateway/coordination 已有 |
| R-07 | Claude 独立 Subagent 上下文 | 最小委派上下文、来源回收 | 不新增递归或写 Agent；复杂任务仍由主 Agent 处理 | Explorer 已有；同源 Plan 委派快照、子循环预检和可解释 collect 诊断已验收，见 [一致性 MVP](SUBAGENT_CONSISTENCY_ACCEPTANCE_2026-10-02.md) |
| R-08 | Hermes memory/session_search | 有界常驻记忆与历史按需检索 | 不引入跨用户历史池；FTS5 可用性需检查 | 复用会话/记忆持久层，后续阶段 |
| R-09 | Hermes Skills 渐进加载 | 验证后候选经验与按需展开 | 不自动发布或激活模型提炼规则；增加审核和失效成本 | 复用既有记忆/Skill 路由，后续阶段 |
| R-10 | 各产品会话继续能力的边界对照 | 分开评价对话/上下文/在途恢复 | 不用摘要、UI、PID 或 lease 到期推断副作用成功 | 原有 journal/fence 保留；决策从当前状态重建；严格恢复与统一恢复/取消结果已验收，显示缓存不授权 |

R-08/R-09 均为专项补充，排在可靠上下文和内核边界之后。只有验证收据、补丁及来源可复核的任务才能生成经验候选；单次“测试通过”不证明经验可以泛化。候选需适用范围、版本/来源、失效条件和审核状态，禁止包含评测答案性信息或 Case 专属修复规则。

## 9. 实施与验收

按 [剩余实施计划](superpowers/plans/2026-10-01-agent-design-reference-improvements.md)推进：P0 规格对账 → 已确认的 Plan MVP → 其他领域逐项确认。原 P1–P4 是候选拆解，后续以各领域的 MVP 讨论结果收敛；选定的批次/记录拆分已落实，其余内核策略迁移和 Hermes 补充继续后置。

本轮 P0 仅完成静态对账和设计记录。已有验收的 Plan/Batch/coordination/Explorer 不重新列为从零建设项目；长任务上下文仍为部分实现。所有新保证必须有直接相关的行为验证，特别是：

- 预算不足或权威状态构建失败时，native/XML 都没有发生模型调用；目标和约束完整。
- 文件外部修改、展开截断和不完整搜索不会满足有效证据/完成条件。
- 决策 supersede、Plan revision 冲突、错误摘要和恢复均使用同一权威状态。
- 拆分后 batch 配对、预算、只读峰值、取消、迟到提交及崩溃窗口行为保持。
- 历史命中无法跨越 workspace 授权或直接获得写权限；经验候选可撤销、可失效。

未来测试按 CLAUDE.md 执行相关用例，失败后精确复验；全量测试和发布级验证须单独授权。只报告实测需求保留、freshness 拒绝、重取、调用/token、重复副作用及耗时，不预设修复率或性能收益。
