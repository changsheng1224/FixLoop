# 设计决策记录（ADR）

> Architecture Decision Records — 记录 FixLoop 的关键取舍，可直接用作面试应答。  
> 架构全貌见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

---

## ADR-001：不使用 LangChain 等 LLM 框架

**Status:** Accepted  
**Date:** M1

### Context

项目目标是「从零构建可审计的 Agent 运行时」。LangChain / LlamaIndex 等框架提供链式抽象与大量集成，但引入隐式魔法、版本漂移和难以逐行调试的控制流。替代方案包括：① 直接用 OpenAI SDK 写脚本；② 自研薄运行时。

### Decision

采用 **标准库 + pydantic + 少量依赖** 手写 Layer 1（~1900 行），Layer 2 在其上组装多 Agent。不引入 LangChain、AutoGen、CrewAI 等框架。

### Consequences

**好处：** 控制循环、工具闸口、Token 预算均可逐行阅读；面试可展示「我懂每一行在干什么」。  
**代价：** 需要自己实现 Provider 适配、会话持久化、Trace 等「框架自带」能力。  
**若将来改变：** 可将 `ModelClient.complete()` 保持为唯一边界，外层再包 LangChain 仅作 Provider 适配层，而不替换 `AgentLoop`。

---

## ADR-002：Agent 用独立实例 + 工厂，而非继承树

**Status:** Accepted  
**Date:** M5

### Context

Multi-Agent 需要 Localizer / Patcher 等不同工具集与 Prompt。替代方案：① `class LocalizerAgent(Agent)` 子类化；② 同一 `Agent` 类 + 不同 config/tools 的工厂函数。

### Decision

**一个 `Agent` 类 + `create_localizer()` / `create_patcher()` 等工厂**。每个 Agent 是独立实例，持有自己的 tool registry、max_steps、system prompt。Orchestrator 组合多个实例，而非继承。

### Consequences

**好处：** Provider 多态在 `ModelClient` 层，Agent 行为差异在配置层；测试时可单独 mock 任一 Agent。  
**代价：** 工厂函数略重复（已通过 `repair_factory.wire_orchestrator` 收敛）。  
**若将来改变：** 若出现 10+ Agent 类型，可引入 `AgentProfile` dataclass 统一描述，仍不必引入子类。

---

## ADR-003：Token 预算使用 tiktoken

**Status:** Accepted  
**Date:** M2

### Context

上下文窗口有限，需在调用模型前裁剪 history。替代方案：① 按字符数估算；② 按词数估算；③ 使用模型官方 tokenizer（tiktoken / transformers）。

### Decision

使用 **tiktoken** 计算 prompt token 数，`ContextManager` 在超预算时按 section 优先级裁剪，必要时调用轻量模型做摘要。

### Consequences

**好处：** 与 OpenAI/DeepSeek 类模型的计费口径接近；裁剪决策可预测。  
**代价：** 多一个依赖；非 OpenAI 系列模型 token 数为近似值。  
**若将来改变：** 抽象 `TokenCounter` 接口，按 provider 注入不同实现，tiktoken 作为默认 backend。

---

## ADR-004：Layer 2 用 RepairState 直传，Blackboard 为辅

**Status:** Accepted  
**Date:** M5

### Context

多 Agent 需要共享定位结果与检索上下文。替代方案：① 纯消息传递（Agent A 的输出字符串喂给 Agent B）；② 中央 Blackboard；③ Orchestrator 持有的结构化 `RepairState`。

### Decision

**主路径：`Orchestrator` 持有 `RepairState`，各阶段读写 typed dataclass**（`SuspectLocation`、`RetrievedContext` 等）。Blackboard 实现冲突检测，但不替代主状态流。

### Consequences

**好处：** Agent 输出必须 parse 成 JSON → 结构化字段，Orchestrator 可校验 schema_version；比自然语言管道更可靠。  
**代价：** 新增字段需改 `state.py` 与序列化；Blackboard 对部分场景冗余。  
**若将来改变：** 若 Agent 数量增至 8+ 且并行写入增多，可将 Blackboard 提升为一等公民，RepairState 改为 Blackboard 快照。

---

## ADR-005：Skill 策略——M5 用字典，稳定后迁 YAML

**Status:** Accepted（已演进）  
**Date:** M5 → M5 Guide

### Context

Orchestrator 需根据 Issue 类型注入修复策略（建议工具、示例补丁）。M5 初期要快：替代方案 ① Python dict `SKILL_REGISTRY`；② YAML 文件；③ 数据库存储。

### Decision

**M5 阶段用 Python 字典**硬编码 4 个 Skill，零解析开销、单测简单。模式稳定后 **迁移为 `src/skills/*.yaml`**，由 `_match_skill()` 按 `trigger_pattern` 匹配。

### Consequences

**好处：** 早期迭代快；YAML 阶段非工程师也可改策略，策略与机制分离。  
**代价：** 存在短暂「dict → YAML」双轨历史；YAML 尚未热加载（需重启）。  
**若将来改变：** 实现 `watchdog` 热加载 + `priority` 字段解决多 pattern 冲突；Skill 命中写入 `node_timings` 供 M7 分析。

---

## ADR-006：Docker 验证容器默认关闭网络

**Status:** Accepted  
**Date:** M6

### Context

Verifier 在容器内跑 `pytest`，需隔离宿主机环境。容器若开网络，恶意或被投毒的依赖可能在验证阶段外联。替代方案：① `network_mode=bridge`；② `network_mode=none`；③ 自定义 seccomp profile。

### Decision

`SandboxManager.create()` 使用 **`network_mode="none"`**，配合 `mem_limit=4g`、`cpu_quota=200000`。依赖应在镜像构建阶段安装完毕。

### Consequences

**好处：** 验证 Turn 无法外联，降低供应链攻击面；行为确定。  
**代价：** 无法在容器内 `pip install` 新依赖；Case 必须自包含。  
**若将来改变：** 可按 profile 分级：`python-offline`（none）vs `python-network`（显式 opt-in + 域名白名单）。

---

## ADR-007：评测集 10 Case，而非 36+

**Status:** Accepted  
**Date:** M7

### Context

需要可复现的 Fix Rate 对比。SWE-bench 等基准有数百 Case，但单次 API 成本高、调试周期长。替代方案：① 10 个微型 Case；② 36 Case 对齐某论文；③ 仅 3 个 demo 不做 formal eval。

### Decision

构建 **10 个微型 Python repo**（5 种错误类型 × 2–3 难度），每个含 `expected_patch.diff` 与 `min_lines.txt`。消融实验 2 变体 × 10 × 3 = 60 runs（可扩展 no_retriever）。

### Consequences

**好处：** 本地几小时可跑完全部；Case 人工可验证；适合 portfolio 展示。  
**代价：** 样本小，full vs single 差距可能未达 +15pp；个别 Case 偶发失败（如 case_006 rep=1）。  
**若将来改变：** 按类型增量添加 Case_011+，保持 `eval/runner.py` 接口不变；基线报告用 `regression_check` 门禁。

---

## ADR-008：Semantic Memory 使用本地 sentence-transformers

**Status:** Accepted  
**Date:** M4

### Context

Episodic / Durable 记忆是关键词匹配，Recall 能力有限。替代方案：① 调用 OpenAI Embedding API；② 本地 `sentence-transformers`；③ 不用语义记忆。

### Decision

使用 **`sentence-transformers` 本地模型**（支持 HF 镜像），向量存 `.agent/semantic/`，cosine 检索。API 不可用时降级为关键词匹配。

### Consequences

**好处：** 无 embedding API 费用；离线可用；隐私友好。  
**代价：** 首次下载模型 ~400MB；CPU 推理比 API 慢。  
**若将来改变：** 抽象 `EmbeddingBackend`，配置切换 local / openai；小仓库可默认关闭 semantic 以减依赖。

---

## ADR-009：Trace 使用 JSONL 追加写

**Status:** Accepted  
**Date:** M3

### Context

需要记录每次 tool call / model turn 供调试与 replay。替代方案：① 单 JSON 文件每次重写；② JSONL 追加；③ SQLite；④ 只打 stderr 日志。

### Decision

每次 run 在 `.agent/runs/{timestamp}/trace.jsonl` **逐行追加 JSON 事件**（tool_start、tool_end、model_response 等）。`run_store` 原子写 `report.json` / `task_state.json`。

### Consequences

**好处：** 崩溃不丢已有 trace；可 `tail -f`；`ReplayRunner` 顺序回放简单。  
**代价：** 大 run 文件变长；需按 run 目录分割（已做）。  
**若将来改变：** 可后台压缩旧 trace 为 `.jsonl.gz`；或导入 OpenTelemetry，JSONL 保留为 debug 模式。

---

## ADR-011：Canonical Trace 事件信封

**Status:** Accepted  
**Date:** 2026-08-05

### Context

ADR-009 已锁定 JSONL 追加，但事件仅为 `{event, created_at, payload?}`，缺少跨 Agent 的 Span 父子关系与统一 status，不利于还原执行树与后续 Langfuse 适配。

### Decision

1. **schema_version=`1`**：在保留 `event`/`created_at` 的前提下，写入  
   `run_id, trace_id, span_id, parent_span_id, event_type, timestamp, status, seq`。  
2. **v1：`trace_id == run_id`**（一次 repair 一条 Trace）。  
3. **Span**：`ContextVar` 栈；`repair_started` 推 root；`agent_ask_started/finished` 推/弹 phase span；普通事件继承当前 span。  
4. **status**：`ok | error | cancelled | unset`；结束类事件由事件名/payload 推断。  
5. **唯一增强写入点**：`RunStore.append_trace_event`（失败时降级为旧三字段，不阻塞主任务）。  
6. **脱敏**：payload 继续走 `redact_artifact`；信封禁止写入 token/密钥。  
7. **实现**：`agent_runtime/canonical_trace.py`；产品说明 `docs/CANONICAL_TRACE.md`。

### Consequences

**好处：** 可用 `run_id` + `seq`/`timestamp` 还原顺序；可构建父子 Span 树；为功能2（Langfuse）提供稳定契约。  
**代价：** 每行 JSON 略大；旧 golden 测试若整行精确匹配需放宽。  
**兼容：** 无新字段的历史 JSONL 仍可按行序回放；校验器对旧行可 `require_canonical=False`。

---

## ADR-010：PatchApplier 采用文件级回滚

**Status:** Accepted  
**Date:** M6

### Context

容器内连续 apply 多个 patch 时，中间失败需恢复一致状态。替代方案：① Git commit 每个 patch；② 文件级 `.bak` 备份；③ 整 repo tar 快照。

### Decision

**每个文件 patch 前备份为 `.bak.{timestamp}`**，任一 patch 失败则 `_revert_all` 逆序恢复。限制：单轮最多 5 patch、单 patch 最多 50 行。

Host 侧 verify 则由 Orchestrator **`_snapshot_repo` / `_restore_repo_snapshot`** 做整目录文本快照（M7D5）。

### Consequences

**好处：** 不依赖容器内 git；回滚逻辑可预测；与 entrypoint.sh 脚本配合简单。  
**代价：** 大文件多 patch 时备份占磁盘；不做 hunk 级三方 merge。  
**若将来改变：** 可统一为 git stash per turn；Docker 与 host 共用 `PatchApplier` 接口，Orchestrator 只调一种回滚策略。

---

## ADR-012：OpenCode 为主参照、Pi 为内核参照、Hermes 为专项补充

**Status:** Accepted（Plan MVP 已实现并通过相关验收；其余方向待分别确定）

**Date:** 2026-10-01

### Context

FixLoop 已有证据驱动 Plan、工具批次、只读 Explorer 和并发恢复；当前缺口集中于必需上下文可能被裁剪、决策缺少版本/证据链，以及循环承担过多修复职责。部分旧规格仍写未实现，容易重复建设。对照来源、本地源码及已验收范围见 [五维分析](AGENT_DESIGN_REFERENCES_2026-10-01.md)。

### Decision

采用 OpenCode 的角色/权限/按需探索组织，Pi 的上下文变换/模型消息转换/工具生命周期边界，Hermes 的历史检索和经验渐进加载；Codex 与 Claude Code 用于交叉检查。继续使用 Python 与 Layer 1/Layer 2，保留 FixLoop 自有 journal、收据、generation fence 和 uncertain 规则。

实施顺序按本轮讨论调整为规格对账 → [Plan MVP](superpowers/specs/2026-10-01-plan-view-replan-decision-mvp.md) → 其他领域逐项确认；内核拆分和历史检索/经验补充后置，详见 [剩余计划](superpowers/plans/2026-10-01-agent-design-reference-improvements.md)。Plan 本轮只补统一计划视图与重规划决策入口，首版接已确认代码验证失败。后续扩展现有 LongTaskState，不另建任务状态；模型提出计划，reducer 依据证据推进，UI/摘要只作投影。

### Consequences

**收益：** 状态来源、执行权限和恢复依据清楚；复用已有能力，后续增量可逐项验证。

**代价：** 必需项门禁可能增加阻断/重取；内核拆分需保持 call 配对、预算和取消契约；历史检索增加授权、索引与失效维护。

**暂缓：** TypeScript 内核替换、通用 DAG 引擎、多个写 Agent、全仓图数据库、新 HTTP 服务及经验自动激活。会话恢复不能证明副作用可重放，不承诺 exactly-once。

**验证边界：** 初次静态审计后，Plan MVP 已完成 [相关验收](PLAN_VIEW_REPLAN_ACCEPTANCE_2026-10-01.md)，包含真实 host pytest 失败→回滚→重规划链路；其余领域仍待讨论，不把上游功能或受控 fixture 数字写成线上修复率提升。

---

## ADR-013：检索语义贯通与消费时源码复验

**日期：** 2026-10-01；**状态：** 已实现，相关验收通过。

**问题：** 工具元数据中的检索范围、完整性和降级原因没有完整贯通 Observation 与模型消费；普通历史或展开结果可能绕过已有局部片段的版本校验。

**决定：** 复用 RetrievalResult 与 Observation 持久层，XML/native/显式展开/Plan 输入使用同一检索说明；消费时有界校验已记录依赖与 scope/blob，区分 freshness 和 completeness。失效正文替换为诊断，保留调用配对与执行错误反馈；XML 续接使用引用。原始历史保留审计，不增加图索引或重规划触发。

**取舍：** 增加有界 I/O；unknown 和超预算结果不能作为当前源码，需要模型授权工具重取。哈希检查不证明新增引用者不存在，也不是原子源码快照；失效时允许重建封印历史。沿用五维参照中的 OpenCode/Pi 消费边界，Hermes 补充继续后置。实现、范围限制与 164 个相关通过节点见 [验收记录](CODE_EXPLORATION_CONSUMPTION_ACCEPTANCE_2026-10-01.md)。

---

## ADR-014：完整必需上下文先占预算，恢复从当前 Plan 重建

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP，完整原规格继续部分后置。

**问题：** state 两次填充可能覆盖并重复计量；目标/约束可能被裁剪，构建异常可能被吞掉；恢复缓存未必代表当前节点。

**决定：** 复用 PlanSession/LongTaskState，生成一次只读权威投影。完整执行与角色规则、目标、约束、节点及当前请求先占预算，失败产生 context_blocked 终态，XML/native 均拒绝发送。native schema 使用真实协议，通用调用示例不常驻 Plan 请求。checkpoint 封装状态引用，owner/journal 对账后重建当前 Plan 投影；确认后态收据避免重放编辑。

**取舍：** 不增预算、不新增数据库或自动决策抽取。超长原文先明确阻断；决策版本、全部入口迁移和内核拆分后置。校验增加既有 evidence ledger 的扫描成本，不能证明文件系统原子一致性。采用五维参照 R-02/R-03/R-10 的投影、预留和恢复边界，真实行为证据与限制见 [长任务上下文验收](LONG_TASK_CONTEXT_ACCEPTANCE_2026-10-01.md)。

---

## ADR-015：统一调用前组装与可选段弹性预算

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP。

**问题：** XML/native 的恢复指令、工具尾部和协议编码分散在循环内，早期 manifest 不能准确表示最终请求；固定源码配额无法借用其他可选段空闲额度。

**决定：** 主循环使用同一 `prepare_request()`，显式传入协议参数，先保护权威任务/Plan 及执行规则。Plan 可选段采用软配额、回收池和当前节点优先级，源码与工具批次按完整单位选择。编码后重新计量，只舍弃可选内容；最终 manifest/checkpoint 保存请求 hash、预算与选中/舍弃引用，调用前再校验 hash 和权威状态。

**取舍：** 沿用 Pi 投影/编码边界和 OpenCode/Pi 裁剪/预留职责区分；弹性算法为本地设计。总上限不变，不做全局最优装箱、额外 LLM 决策或自动重取。普通 L1 固定配额与恢复契约保持；记忆候选 ID 不等于完整文本入选，最终段 hash 与请求 hash 才对应发送内容。SDK 服务端 token 由真实 usage 校准。范围、行为证据及后置项见 [上下文组装验收](CONTEXT_ASSEMBLY_ACCEPTANCE_2026-10-02.md)。

---

## ADR-016：证据谓词解释与当前节点必需事实摘要

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP。

**问题：** 布尔校验不能解释证据为何不可用；正文全部舍弃后，当前节点可能只有引用而缺少事实说明；校验通过容易被误认为正文已进入模型请求。

**决定：** 复用 EvidenceLedger，增加类型、状态、原因及当前/历史用途解释，布尔接口继续由原有谓词决定。四类持久事实直接投影为必需摘要，逐字段有界并显式标明省略；完整记录和版本映射保留，模型仅使用短 checksum 显示标识。最终 manifest 分别记录校验、摘要和正文入选，checkpoint 深拷贝；同一引用不同用途分别计量。显式 native 尾部引用加入既有正文校验候选。

**取舍：** 增加必需 token 与既有快照检查成本，总预算保持。片段新鲜、搜索完整、事实记录有效和模型理解分别评价；确认补丁的历史输入不因当前源码变化而重放。采用 R-02/R-03 的职责边界，具体格式为本地设计。没有额外摘要模型、自动重取/重规划或新数据库；适用范围、真实行为验证及限制见 [证据消费验收](EVIDENCE_CONSUMPTION_ACCEPTANCE_2026-10-02.md)。

---

## ADR-017：显式决策版本与恢复时的只读活动投影

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP。

**问题：** 长任务决策只有追加字典，缺少版本和证据关联，且与来源复核/证据替换混用；主循环未消费决策，checkpoint 嵌套投影可能受后续修改影响。

**决定：** 复用现有持久容器和 journal，正式决策通过 owner API 显式创建/替换，绑定节点、Plan version、证据引用及 checksum。旧版保留，superseded/needs_review 由只读投影推导；当前节点仅消费有效最新版，证据进入既有必需摘要。来源复核与证据替换继续只作审计事实。checkpoint 深拷贝并保存决策版本引用，seal 的任务快照核对 journal 前缀；恢复从当前事实重建，确认后态不因旧决策失效重放。

**取舍：** 必需 token 与快照 I/O 增加，默认总上限保持；长决策明确阻断，不自动摘要。source 为声明的审计引用，不替代授权；首版只有节点作用域和 owner API，自动提炼、语义冲突裁决、消息来源注册及全入口迁移后置。采用 R-02/R-03/R-10 的职责与恢复边界，具体版本链为本地设计。行为证据、适用范围与限制见 [决策投影验收](DECISION_PROJECTION_ACCEPTANCE_2026-10-02.md)。

---

## ADR-018：工具批次编排与 Observation 记录的显式边界

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP。

**问题：** 请求准备和批次调度虽已有独立模块，主循环仍直接承担批次准备/执行/归并接线、Observation 入库和结果引用关联，与修复策略混杂。

**决定：** 抽出 ToolBatchRunner 和 ToolObservationRecorder，以明确依赖及有类型的 owner 回调接入，不传递整个 AgentLoop。XML/native 共用记录入口，native 批次复用已有 Scheduler、Executor、Plan 操作及读许可；结果按 call ID/ordinal 归并并返回明确引用。记录器绑定实际运行 owner，存储失败仍关闭句柄，结果引用仅在持久化成功后关联。工具完成事件不提升为执行恢复授权。

**取舍：** 保留两个协议循环、生成器式步骤和既有修复策略，降低取消/恢复窗口迁移风险。新增模块无 src 依赖，但 Loop 的 edit-lock/grounding 依赖及 repair 专属策略尚未迁移；无新调度器、插件系统、持久层或跨存储原子保证。采用 Pi R-02/R-05 与 OpenCode R-06/R-10 的职责和事件边界，具体接口为本地实现。实际行为验证及限制见 [内核拆分验收](KERNEL_SPLIT_ACCEPTANCE_2026-10-02.md)。

---

## ADR-019：native 批次纯参数预检与原始协议完整性

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP。

**问题：** 批次内参数错误要到执行步骤才暴露，provider 将非法 input 改为 `{}` 会丢失异常；原始内容与规范化调用缺少整批配对核验。

**决定：** 派发前按冻结 schema 对全部调用做纯预检，保留原始参数及身份。schema 错误单项返回配对拒绝，不创建执行 Action/Plan operation 或预约调用预算；合法兄弟继续。原始 content 可用时核对数量、次序、ID、名称和输入；结构/身份错误整批零执行。provider 保留非法 input，Executor 仍负责规范化、授权和最终状态门禁。

**取舍：** 复用既有 validator 与 adapter 的无原始 content 契约，不引入校验引擎或兼容层。沿用截断回合全部丢弃，不扩大并行白名单、不新增 XML 批次、事务/回滚、DAG 或重试控制器。Pi R-05 提供生命周期参照，OpenCode R-06 提供执行权限边界；具体协议检查为本地实现。行为与恢复限制见 [工具批次预检验收](BATCH_PREFLIGHT_ACCEPTANCE_2026-10-02.md)。

---

## ADR-020：严格恢复意图与统一恢复/取消诊断投影

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP。

**问题：** 显式恢复缺失/损坏 checkpoint 可能进入普通执行；资源详情、Plan 恢复与取消结果分散，取消请求和清理预备结果容易被误当作终态。

**决定：** 新执行指定 run_id 与严格 resume_run_id 互斥。严格校验 envelope、身份、任务原文及消费字段后才进入 owner/reconcile；失败零执行且保留原文件。从当前协调存储与 Plan 报告生成脱离源对象的 recovery_outcome，公开状态、进度和报告消费同一投影；恢复丢弃旧显示缓存。按持久状态区分 cancel_requested、cancelling、cancelled，清理与恢复涉及的副作用核验分别表达。清理失败释放 owner 后，同 generation 重复取消只返回已有结果，旧 generation 仍拒绝。

**取舍：** 复用既有恢复器、收据与回滚 fence；投影不授权、不驱动自动重试、不增加状态机。采用 OpenCode 的控制/事件边界和 Pi 的取消生命周期，接口和错误码是本地设计；Hermes 能力及其余入口迁移后置。普通 checkpoint 探测契约保留，严格恢复不迁移旧 schema 或自动改变目标。范围、实际进程与 host pytest 验证及限制见 [恢复取消验收](RECOVERY_CANCEL_ACCEPTANCE_2026-10-02.md)。

---

## ADR-021：只读 Subagent 的同源计划上下文与可解释消费

**日期：** 2026-10-02；**状态：** 已实现本轮确认的 MVP。

**问题：** 子任务只有 Plan 标识而缺少目标/约束/节点内容，Explorer native 入口未复用新预检，freshness 的布尔值和异常类型不够解释主 Agent 的下一步选择。

**决定：** 从同一 plan_view 派生有界委派快照，完整保存目标、硬约束和当前节点；超限在任务创建/本批预算预留前拒绝。快照绑定父 task/run/workspace，恢复保留快照并重新核验结果。Explorer 复用 Layer 1 纯协议/schema 预检，原始内容可用时完整核对，协议错误整批零读取、单项参数错误配对拒绝，合法兄弟串行执行。collect 提供校验/执行/清理原因，目标或约束变化使旧结果失效；发现仍是候选，重读只形成 source_review 审计。

**取舍：** OpenCode 提供聚焦探索/权限参照，Pi 提供上下文与执行边界，Claude 提供独立子上下文；8000 字节上限、错误码及 generation/receipt 门禁为本地设计。只复用 Batch 数据预检，不接入完整 Scheduler/AgentLoop；子任务固定尝试上限继续计入拒绝项。保留两个只读任务、全 workspace freshness 和清理门禁，不新增递归/写 Agent/自动决策/自动重试。Hermes 历史检索与经验候选后置。实际验证与限制见 [Subagent 一致性验收](SUBAGENT_CONSISTENCY_ACCEPTANCE_2026-10-02.md)。

---

## 索引：面试常见问题 → ADR

| 问题 | 参见 |
|------|------|
| 为什么不用 LangChain？ | ADR-001 |
| 多 Agent 怎么通信？ | ADR-004 + ARCHITECTURE §5 |
| Token 怎么控？ | ADR-003 |
| 工具怎么防越权？ | ARCHITECTURE §6 + ADR-006 |
| 评测数据可信吗？ | ADR-007 + README 指标表 |
| 怎么调试 Agent？ | ADR-009 + ADR-011（Canonical Trace） |
| 补丁失败怎么办？ | ADR-010 + ARCHITECTURE §5.3 |
| 借鉴哪些 Agent，为什么不直接替换内核？ | ADR-012 + 五维设计参照 |
| 检索成功为何不等于证据新鲜或搜索完整？ | ADR-013 + 消费链 MVP 验收 |
| 压缩和恢复如何保留目标与约束？ | ADR-014 + 长任务上下文 MVP 验收 |
| 可选上下文如何借预算，manifest 为何在编码后生成？ | ADR-015 + 上下文组装 MVP 验收 |
| 证据有效为何不等于正文已进入请求？ | ADR-016 + 证据消费 MVP 验收 |
| 决策如何替代，恢复为何不用旧缓存直接继续？ | ADR-017 + 决策与恢复投影 MVP 验收 |
| 主循环、批次和结果记录如何分工？ | ADR-018 + 内核职责拆分 MVP 验收 |
| 批次参数错误和原始协议不一致如何处理？ | ADR-019 + 工具批次预检 MVP 验收 |
| 恢复失败为何不能转为新执行，取消何时算完成？ | ADR-020 + 恢复与取消 MVP 验收 |
| 子 Agent 如何消费同一份计划，何时拒绝其结果？ | ADR-021 + Subagent 一致性 MVP 验收 |
