# 2026.08.10—2026.08.16 Agent 求职增强执行计划

## 一、本周目标与执行边界

本周围绕五条主线推进：

1. **Agent 题目**：新增模型与 Agent 协同、产品体验与人机信任，补强评测、安全、Coding Agent、框架与行业认知，并完成 SWE-bench、LLM Wiki 和知识图谱变体调研。
2. **RAG 题目**：完成失败归因、容量成本、评测门禁、GraphRAG、线上诊断及 Skill Router 协同六类专项补强。
3. **FixLoop 实验与评测**：完成代码定位对照、多模型能力矩阵、主流 Coding Agent 横向对比、Trace 数据飞轮和 SWE-bench Verified 持续迭代。
4. **FixLoop 题目**：新增代码理解、代码编辑、模型与产品对照，补强 Runtime、Multi-Agent、Tool/MCP、Trace/Benchmark 等项目深挖题。
5. **面试表达**：建立 20 秒与 2 分钟双版本回答体系，固化三类表达模板、三条个人标签、技术取舍、高压追问和现场状态管理。

执行边界：

- Agent、RAG 和 FixLoop“题目补充”只产出问题、答案、架构图、案例和口述材料，不安排实现。
- FixLoop“实现补充”才进行编码、实验、评测和 Trace 数据处理。
- Harness 优化必须解决跨 Case 的通用问题，禁止读取 Gold Patch 后特调。
- Verified 固定 Manifest 后才能比较版本；模型、Prompt、预算、Tool、代码和 Harness 版本必须完整记录。
- Verified 全量 500 题属于预算允许时的最终验证，不以牺牲预评测稳定性和结果审计为代价。

## 二、每日时间安排

- **上午 8:30—12:00**：专项题、调研、实验设计和失败分析。
- **下午 13:30—18:30**：FixLoop 实验、Harness 迭代、测试和正式评测。
- **晚上 19:30—22:30**：RAG/FixLoop 题目、长任务批跑和面试表达训练。

每个时间块最后 30—45 分钟用于：

- 运行相关测试并保存结果；
- 更新 Manifest、Trace、实验表和失败分类；
- 将当天新增题目转成 20 秒与 2 分钟版本；
- 记录阻塞项及次日唯一最高优先级。

## 三、SWE-bench 分层评测方案

| 层级 | 数据量 | 目的 | 使用规则 |
|---|---:|---|---|
| 自建功能回归集 | 20 | 快速验证工具、Loop、Patch、Verifier 和环境兼容性 | 每次通用 Harness 修改后运行 |
| SWE-bench Lite dev | 23 | 开发 Benchmark Adapter 和端到端接入 | 可重复运行，不作为最终成绩 |
| SWE-bench Verified 预评测集 | 固定 50，稳定后扩至 100 | 建立 Baseline、迭代 Harness、估算效果与成本 | 第一次运行前固定 Manifest，不因结果替换 Case |
| SWE-bench Verified 全量 | 500 | Harness 冻结后的最终验证 | 预算与时间允许时运行，禁止中途改配置 |

Manifest 至少锁定：

- Case ID 与数据版本；
- 模型、参数和服务版本；
- System Prompt、Agent Prompt 和模板版本；
- Context、Token、Steps、Timeout 和成本预算；
- Tool、Skill、Sandbox 和 Verifier 版本；
- FixLoop Commit、Harness Commit、镜像和依赖版本。

评测统一报告：

- Resolved Rate 与有效提交数；
- 环境成功率和 Patch 可应用率；
- Token、延迟、成本和 Steps；
- Tool、定位、Context、Patch、Verifier、模型和环境失败分布；
- 人工介入情况及是否存在结果排除。

## 四、每日执行计划

### 8 月 10 日（周一）｜研究、回归基线与回答体系

#### 上午｜Agent 题目：模型—Agent 协同与能力归因；SWE-bench 调研

- [ ] 基础模型能力与 Agent Harness 分别决定哪些结果。
- [ ] 如何区分模型、Prompt、Context、Tool、Loop、Verifier 和环境问题。
- [ ] 换模型能解决什么，什么问题必须由系统层处理。
- [ ] 如何用固定 Harness 的模型对照和固定模型的 Harness 消融完成归因。
- [ ] 小模型适合承担哪些路由、摘要、检查和分类阶段。
- [ ] 模型升级后为什么仍需固定 Manifest 回归。
- [ ] 调研 SWE-bench、Lite、Verified 的任务结构、数据规模和评测方式。
- [ ] 调研 Repository、Issue、Gold Patch、Test Patch、Base Commit 和官方 Harness 的关系。
- [ ] 调研 Resolved 判定、环境失败、Patch 失败和过拟合风险。

验收物：

- [ ] 8—10 道“模型—Agent 协同与能力归因”题；
- [ ] 一张能力归因矩阵；
- [ ] 一份 SWE-bench 调研笔记；
- [ ] “为什么选择 Verified、如何避免过拟合”的 2 分钟回答。

#### 下午｜FixLoop：20 Case 功能回归与 Lite dev 接入复验

- [ ] 固定 20 个自建 Case，覆盖读取、搜索、编辑、测试、重试、取消和 Verifier。
- [ ] 运行功能回归，修复通用接口、环境和 Patch 导出问题。
- [ ] 复跑 Lite dev 23 题，确认 Benchmark Adapter 可重复工作。
- [ ] 校验每题均保存 Manifest、Trace、Patch 和官方 Harness 报告。
- [ ] 区分环境失败、Agent 失败和评测失败。
- [ ] 冻结 Benchmark Adapter v1。

验收物：

- [ ] 20 Case 功能回归报告；
- [ ] Lite dev 23 题接入报告；
- [ ] Benchmark Adapter v1；
- [ ] Verified 预评测运行前检查清单。

#### 晚上｜RAG 题目：失败归因与模型协同；面试：回答体系

- [ ] RAG 失败如何拆为 Query、路由、Filter、召回、融合、Rerank、Context、生成和索引问题。
- [ ] 如何区分 Embedding、Reranker、LLM 能力不足与 Pipeline 设计问题。
- [ ] 换模型、调 Prompt、改检索、补数据和加 Verifier分别适合什么问题。
- [ ] 如何使用 Trace、离线评测、线上反馈和消融实验定位根因。
- [ ] 为核心问题建立“20 秒：定义 + 核心结论”版本。
- [ ] 为核心问题建立“2 分钟：依据 + 实现 + 取舍”版本。
- [ ] 分别使用通用概念题、项目深挖题和系统设计题模板演练 3 题。

验收物：

- [ ] 8—10 道 RAG 失败归因专项题；
- [ ] 一张 RAG 失败归因树；
- [ ] 6 道核心题的双版本回答；
- [ ] 三类回答模板卡片。

### 8 月 11 日（周二）｜Verified Baseline 与评测补强

#### 上午｜Agent 题目：评测与质量保障；FixLoop 题目：Trace 与 Benchmark

Agent 题目：

- [ ] Agent Evaluation 的离线评测、线上监控、人工评审和发布门禁如何组成闭环。
- [ ] Task Success、Tool、Skill、Context、Memory、轨迹、安全、性能和成本如何分层评测。
- [ ] LLM-as-Judge、规则、Verifier 和人工标注如何组合。
- [ ] Holdout、Manifest、基线、门禁、消融和统计显著性如何使用。
- [ ] 如何避免 Benchmark 污染、Judge 偏见和只优化最终成功率。

FixLoop 题目：

- [ ] Trace 的事件模型、父子 Span、脱敏、采样和复现。
- [ ] Benchmark 接入、Manifest 锁定、结果归因和可信报告。
- [ ] Trace 如何驱动 Bad Case 数据飞轮。
- [ ] SWE-bench 少量任务的结果如何表述，不能得出什么结论。

验收物：

- [ ] 10—12 道 Agent 评测专项题；
- [ ] 6—8 道 FixLoop Trace/Benchmark 深挖题；
- [ ] 一张“离线评测—门禁—线上监控—数据飞轮”图。

#### 下午｜FixLoop：Verified 50 Baseline

- [ ] 第一次运行前固定 Verified 50 Case Manifest。
- [ ] 执行 5—10 题 Smoke Test，只修复纯基础设施问题。
- [ ] 使用冻结配置运行 Verified 50 Baseline。
- [ ] 运行期间不临时修改 Prompt、Tool、预算或 Steps。
- [ ] 汇总 Resolved Rate、环境成功率、Patch 可应用率、Token、耗时和 Steps。
- [ ] 按模型、定位、Context、Tool、Patch、Verifier 和环境生成失败分类。

验收物：

- [ ] Verified 50 Baseline 报告；
- [ ] 每题 Trace、Patch、Manifest 和 Harness 结果；
- [ ] 第一版失败 Pareto 图；
- [ ] 影响范围最大的 3 个通用 Harness 问题。

#### 晚上｜RAG 题目：评测与发版门禁；面试：高压追问第一组

- [ ] 检索 IR、RAGAS、端到端任务成功和业务指标如何组合。
- [ ] Manifest、Holdout、同版本复跑、硬门禁和软告警如何设计。
- [ ] 索引数据面、检索服务和生成链路分别如何验收。
- [ ] 灰度 HOLD、风险接受、回滚和紧急 Hotfix 如何处理。
- [ ] 演练“任职时间短”“指标是否个人完成”“SWE-bench 样本少”“指标是否可信”。
- [ ] 回答时先承认合理质疑，再用职责范围、代码、Trace、实验和数据说明。

验收物：

- [ ] 8—10 道评测与发版门禁题；
- [ ] 一张 RAG 发布门禁流程图；
- [ ] 4 道高压追问的双版本回答。

### 8 月 12 日（周三）｜代码定位实验与知识图谱调研

#### 上午｜Agent/FixLoop 题目：Coding Agent、代码理解与代码编辑

Agent 题目：

- [ ] Coding Agent 如何探索陌生仓库、选择 Context、生成 Patch 和验证修复。
- [ ] Grep、AST、LSP、Repo Map、Embedding 和代码图分别适合什么场景。
- [ ] 为什么代码 Agent 仍需要确定性搜索和测试，而不是完全依赖大模型。
- [ ] 编辑策略、Patch 边界、Git Worktree、回滚、冲突和提交如何设计。
- [ ] 如何判断代码修复完成，Verifier 需要哪些证据。

FixLoop 题目：

- [ ] 新增“代码理解与仓库探索”专题。
- [ ] 新增“代码编辑与 Git 工程”专题。
- [ ] 补强“Agent Runtime 与 Loop”中的探索、编辑、验证和终止链路。

验收物：

- [ ] 10—12 道 Coding Agent 与工程实践题；
- [ ] 8—10 道 FixLoop 代码理解/编辑题；
- [ ] 一张仓库探索到验证的完整链路图。

#### 下午｜FixLoop：代码定位方法对照实验与 Harness Iteration 1

- [ ] 固定 10—15 个真实定位 Case。
- [ ] 分别运行 Stack Trace、Grep/Ripgrep、文件路径规则、AST/LSP 和 Embedding。
- [ ] 统计正确文件 Recall、候选文件数量、噪声和耗时。
- [ ] 分析单方法失败类型和组合收益。
- [ ] 实现或优化“确定性定位 → 符号关系扩展 → 语义检索兜底”。
- [ ] 优化 Repo Map、搜索结果预算、Context 选择和 Patcher 输入证据。
- [ ] 运行 20 Case 回归，并在 Verified 50 上复跑 Iteration 1。

验收物：

- [ ] 代码定位实验数据表；
- [ ] 五种方法的适用边界和失败案例；
- [ ] 组合定位策略；
- [ ] Baseline 与 Iteration 1 对照报告。

#### 晚上｜RAG 题目：GraphRAG 效果与项目边界；知识图谱变体调研

- [ ] GraphRAG 相比 Chunk 检索解决哪些关系、多跳、全局主题问题。
- [ ] Local、Global、社区摘要、Entity、Relation 和 Seed Entity 如何评测。
- [ ] Graph 路提升如何做对照、消融和失败归因。
- [ ] 华为项目中建图、发版、图治理和线上指标哪些属于个人职责，哪些未参与。
- [ ] 调研属性图、RDF/三元组、知识图谱、事件图、代码图和时序图等变体。
- [ ] 调研不同图表示在 RAG、代码理解和 Data Agent 中的适用场景与代价。
- [ ] 演练“GraphRAG 哪些部分不是你做的”。

验收物：

- [ ] 8—10 道 GraphRAG 效果与边界题；
- [ ] 一份知识图谱变体对照表；
- [ ] 一张 GraphRAG 评测与消融图；
- [ ] 项目边界的诚实口径。

### 8 月 13 日（周四）｜多模型矩阵与安全补强

#### 上午｜Agent/FixLoop 题目：安全、沙箱、审批、Tool 与 MCP

Agent 题目：

- [ ] 输入、执行和输出三个阶段分别如何做安全审核。
- [ ] 只读、写入、外部副作用工具如何分级授权、审批和取消。
- [ ] Sandbox、Worktree、容器、网络和资源限制分别解决什么问题。
- [ ] Prompt Injection、路径穿越、Secret、PII、租户越权如何防护。
- [ ] HITL、Dry-run、审计和事故追责如何落地。

FixLoop 题目：

- [ ] 补强 Tool 与 MCP 的 Registry、权限、Schema、错误、超时和审计。
- [ ] 补强 Shell、Read、Grep、Write、Test 的攻击面和防护。
- [ ] 准备“沙箱真的安全吗”的边界回答。

验收物：

- [ ] 10—12 道 Agent 安全与审批题；
- [ ] 8—10 道 FixLoop Tool/MCP 安全题；
- [ ] 一张工具风险分级与审批矩阵；
- [ ] 一份沙箱已覆盖与未覆盖风险清单。

#### 下午｜FixLoop：多模型能力矩阵与 Harness Iteration 2

- [ ] 固定同一 Harness、Prompt、任务和预算。
- [ ] 选择 2 个模型，在 5—10 个任务上运行。
- [ ] 对比 Tool 选择准确率、参数正确率、代码定位、Patch 正确率和失败恢复。
- [ ] 对比 Token、延迟和成本。
- [ ] 区分换模型可以解决的问题与必须修改 Harness 的问题。
- [ ] 识别适合交给小模型的阶段。
- [ ] 识别必须交给 Verifier 或确定性规则的问题。
- [ ] 基于通用失败优化 Tool 契约、Structured Output、Patch 反馈、Loop 和 Verifier。
- [ ] 运行 20 Case 回归，并在 Verified 50 上复跑 Iteration 2。

验收物：

- [ ] 双模型能力矩阵；
- [ ] 模型、Harness、小模型和 Verifier 的职责结论；
- [ ] Baseline、Iteration 1 和 Iteration 2 对照；
- [ ] 模型路由或选型建议。

#### 晚上｜RAG 题目：线上检索异常诊断；面试：高压追问第二组

- [ ] 如何从空结果率、召回、延迟、错误、超时和降级识别异常。
- [ ] 如何区分 Query、Filter、ES、Milvus、Graph、Rerank、缓存和索引问题。
- [ ] 如何通过 Trace、Metrics、日志、对账和影子流量定位。
- [ ] 抖动、熔断、降级、缓存陈旧、双写不一致和索引切换如何处理。
- [ ] 演练“FixLoop 是否过度设计”“为什么不用 LangGraph”“Multi-Agent 是否有消融证据”“去掉强模型还剩什么价值”。

验收物：

- [ ] 8—10 道线上检索异常诊断题；
- [ ] 一张检索故障定位决策树；
- [ ] 4 道高压追问的双版本回答。

### 8 月 14 日（周五）｜产品对照、LLM Wiki 与 RAG 价值

#### 上午｜Agent 题目：产品体验、人机信任、框架与行业；LLM Wiki 调研

- [ ] Agent 产品体验除正确率外为何还要关注可控、可解释、可恢复和响应反馈。
- [ ] 何时展示计划、进度、Citation、风险、工具动作和不确定性。
- [ ] 如何设计澄清、确认、撤销、纠错、人工接管和失败后的下一步。
- [ ] 如何避免虚假确定性、自动化偏见和过度打扰。
- [ ] LangChain、LangGraph、自研 Runtime 的适用边界与迁移成本。
- [ ] Cursor、Claude Code、Codex 等产品在探索、编辑、审批和交互上的差异。
- [ ] Agent 框架能力增强后，哪些 Runtime、治理、评测和业务能力仍需自研掌控。
- [ ] 调研 LLM Wiki 的目标、架构、知识组织、生成更新和质量控制方式。
- [ ] 分析 LLM Wiki 与传统 Wiki、RAG 知识库、GraphRAG 的区别与可借鉴点。

验收物：

- [ ] 8—10 道 Agent 产品体验与人机信任题；
- [ ] 8—10 道框架、产品与行业认知题；
- [ ] 一份 LLM Wiki 调研笔记；
- [ ] 一张 Agent 产品信任机制图。

#### 下午｜FixLoop：主流 Coding Agent 横向对比

- [ ] 固定 2—3 个相同任务。
- [ ] 选择 Codex、Claude Code 或 Cursor 中至少 2 个产品进行对比。
- [ ] 对比仓库探索、Context 选择、Tool 调用和编辑策略。
- [ ] 对比循环、失败恢复、人工介入和验证方式。
- [ ] 记录测试结果、Token、耗时和完成质量。
- [ ] 保持相同任务与验收标准，记录产品配置差异。
- [ ] 总结 FixLoop 的设计取舍，不声称全面优于成熟产品。

验收物：

- [ ] 主流 Coding Agent 对照实验表；
- [ ] 一份简短能力对比报告；
- [ ] FixLoop 的优势、代价和适用边界；
- [ ] 可用于面试的 2 分钟产品对照回答。

#### 晚上｜RAG 题目：容量、成本、业务价值与 Skill 协同

容量、成本与业务价值：

- [ ] 文档、Chunk、向量、BM25、Graph 和日志的容量如何估算。
- [ ] Embedding、Rerank、LLM、存储、网络和人工评测成本如何拆分。
- [ ] 如何在 Recall、延迟、成本和新鲜度之间取舍。
- [ ] 如何用任务完成率、人工节省、问题解决时长和业务风险说明价值。
- [ ] 何时应扩大规模，何时应优化质量而不是继续堆数据。

Skill Router 与 RAG 协同：

- [ ] Skill 如何决定数据域、索引、Filter、召回路、参数和输出契约。
- [ ] 一个请求命中多 Skill 时如何编排多个检索流程。
- [ ] 弱命中、无命中、误触发和 Skill 切换如何影响 RAG。
- [ ] Skill 版本与检索配置如何联动评测、灰度和回滚。

验收物：

- [ ] 8—10 道容量、成本与业务价值题；
- [ ] 8—10 道 Skill Router 与 RAG 协同题；
- [ ] 一张 RAG 成本模型；
- [ ] 一张 Skill 到 Retrieve Pipeline 的配置映射图。

### 8 月 15 日（周六）｜Release Candidate、数据飞轮与预评测扩展

#### 上午｜FixLoop 题目：Runtime、Multi-Agent、Verifier、模型与产品实验

- [ ] 补强 Agent Runtime 与 Loop 的状态、终止、No-progress、Retry、Replan 和取消。
- [ ] 补强 Multi-Agent 的适用条件、角色拆分、通信、冲突、成本和降级。
- [ ] 补强 Verifier 的证据、测试分层、误判、反馈和终止权。
- [ ] 新增“模型能力与产品对照实验”专题。
- [ ] 准备如何设计公平实验、控制变量、选择指标和解释不显著结果。
- [ ] 准备模型升级、产品差异和 Harness 改动的归因边界。

验收物：

- [ ] 8—10 道 Runtime 与 Loop 题；
- [ ] 8—10 道 Multi-Agent 与 Verifier 题；
- [ ] 6—8 道模型与产品对照实验题；
- [ ] 一张 FixLoop Runtime 与多 Agent 状态图。

#### 下午｜FixLoop：Trace 数据飞轮与 Verified 50～100 Release Candidate

- [ ] 从 Trace 自动或半自动抽取 Bad Case。
- [ ] 按模型、Context、Tool、Patch、Verifier、环境和编排分类。
- [ ] 选择影响范围最大的通用根因。
- [ ] 将修复策略关联到代码、配置、Prompt 或数据变更。
- [ ] 使用同一 Manifest 复跑并判断 Case 是否关闭。
- [ ] 固化链路：`Trace → Bad Case 抽取 → 失败分类 → 根因定位 → 修复策略 → 同 Manifest 复跑 → Case 关闭`。
- [ ] 执行全量测试和 lint，冻结 Harness Release Candidate。
- [ ] 对固定 Verified 50 进行 RC 复跑；资源稳定时扩展至预先固定的 100 Case。

验收物：

- [ ] Bad Case 数据模型与关闭标准；
- [ ] 至少 5 个完成闭环的 Bad Case；
- [ ] Baseline 至 RC 的 Harness 演进报告；
- [ ] Verified 50～100 预评测报告；
- [ ] 一条可演示的数据飞轮 Trace。

#### 晚上｜面试：三条个人标签、技术取舍与完整模拟

- [ ] 反复强化“生产级 Agent RAG 链路”标签。
- [ ] 反复强化“自研 FixLoop Runtime 与底层机制”标签。
- [ ] 反复强化“Trace、Benchmark 与 Bad Case 数据飞轮”标签。
- [ ] 演练为什么自研 Runtime 而非直接使用 LangGraph。
- [ ] 演练为什么使用 Multi-Agent 而非单 Agent。
- [ ] 演练为什么使用 Worktree 而非复制仓库。
- [ ] 演练为什么保留 BM25 与向量双路。
- [ ] 演练为什么使用分级 Skill Router 而非全部交给 LLM。
- [ ] 每个取舍回答覆盖选择原因、替代方案、未选原因、代价和规模变化后的调整。
- [ ] 完成第一次完整模拟面试。

验收物：

- [ ] 三条个人标签的证据映射；
- [ ] 5 个核心技术取舍回答；
- [ ] 第一次完整模拟面试复盘。

### 8 月 16 日（周日）｜全量评测、结果审计与面试收口

#### 上午｜FixLoop：Verified 全量 500 或结果审计

满足以下条件时启动或完成 Verified 全量 500：

- [ ] Harness Release Candidate 已冻结；
- [ ] 20 Case、Lite dev 和 Verified 50～100 均稳定；
- [ ] 模型额度、Docker、磁盘、时间和成本预算充足；
- [ ] 全量运行期间无需人工介入；
- [ ] 不会为提高成绩中途修改配置。

如果不满足条件，则不强行全量运行，改为：

- [ ] 审计 Verified 50～100 的 Manifest、Trace、Patch 和 Harness 报告；
- [ ] 对齐 Token、成本、延迟、Steps 和有效提交数；
- [ ] 完成定位、多模型和 Coding Agent 三类实验报告；
- [ ] 给出全量 500 的成本预估、并发方案和后续运行计划。

验收物：

- [ ] Verified 全量报告，或可信的 50～100 预评测报告与全量执行计划；
- [ ] 无人工干预和无 Case 特调声明；
- [ ] 可用于简历和面试的准确指标口径。

#### 下午｜面试：双版本题库、未知问题与状态管理

- [ ] 为所有核心题补齐 20 秒结论和 2 分钟标准回答。
- [ ] 通用概念题使用“是什么 → 解决什么问题 → 如何实现 → 常见误区”。
- [ ] 项目深挖题使用“背景约束 → 我的职责 → 方案选择 → 关键动作 → 指标结果 → 复盘边界”。
- [ ] 系统设计题使用“需求澄清 → 指标 → 架构 → 状态数据 → 主链路 → 异常 → 安全评测 → 取舍”。
- [ ] 演练不知道的问题：明确未做范围、已知事实、推断部分和验证方法。
- [ ] 复杂问题先停顿 2～3 秒；边界不清先澄清。
- [ ] 每次回答只保留一个中心结论。
- [ ] 面试官打断时立即停止并回答新问题。
- [ ] 讲远时使用“回到问题本身，我的结论是……”。
- [ ] 系统设计边画边讲数据流、状态流和故障流。
- [ ] 检查回答中的英文术语，首次出现时说明实际作用。

验收物：

- [ ] 核心题双版本索引；
- [ ] 三类回答模板；
- [ ] 未知问题回答模板；
- [ ] 现场状态管理检查表；
- [ ] 去除无解释术语堆砌后的回答抽样。

#### 晚上｜高压追问终测与第二次模拟

- [ ] 你任职时间这么短，为什么能承担 3～5 年岗位？
- [ ] 这些指标真的是你个人做出来的吗？
- [ ] FixLoop 为什么不是一个过度设计的 Demo？
- [ ] 为什么不用 LangGraph 直接实现？
- [ ] Multi-Agent 的收益有没有消融证据？
- [ ] SWE-bench 任务这么少，结果可信吗？
- [ ] 你的沙箱真的安全吗？
- [ ] GraphRAG 哪些部分不是你做的？
- [ ] 如果去掉更强模型，你的 Agent 工程还剩什么价值？
- [ ] 线上出故障时你亲自处理过什么？
- [ ] 完成第二次完整模拟面试。
- [ ] 只修正结论不清、证据不足、边界不诚实和回答超时四类问题。

验收物：

- [ ] 10 道高压追问定稿；
- [ ] 第二次完整模拟面试复盘；
- [ ] 面试前最终证据索引；
- [ ] 次周只需复习、不再大规模补题的核心清单。

## 五、内容覆盖核对表

### Agent 题目与调研

- [ ] 模型—Agent 协同与能力归因；
- [ ] Agent 产品体验与人机信任；
- [ ] 评测与质量保障；
- [ ] 安全、沙箱与人工审批；
- [ ] Coding Agent 与工程实践；
- [ ] 框架、产品与行业认知；
- [ ] SWE-bench 调研；
- [ ] LLM Wiki 调研；
- [ ] 知识图谱变体调研。

### RAG 题目

- [ ] RAG 失败归因与模型协同；
- [ ] RAG 容量、成本与业务价值；
- [ ] 评测与发版门禁；
- [ ] GraphRAG 效果与项目边界；
- [ ] 线上检索异常诊断；
- [ ] Skill Router 与 RAG 协同。

### FixLoop 实验与实现

- [ ] 10～15 个 Case 的代码定位方法对照；
- [ ] 2 个模型、5～10 个任务的能力矩阵；
- [ ] 2～3 个相同任务的主流 Coding Agent 对照；
- [ ] Trace 驱动的 Bad Case 数据飞轮；
- [ ] 20 Case 功能回归；
- [ ] Lite dev 23 题接入开发；
- [ ] Verified 固定 50～100 题预评测；
- [ ] 预算允许时 Verified 全量 500。

### FixLoop 题目

- [ ] 代码理解与仓库探索；
- [ ] 代码编辑与 Git 工程；
- [ ] 模型能力与产品对照实验；
- [ ] Agent Runtime 与 Loop；
- [ ] Multi-Agent 与 Verifier；
- [ ] Tool 与 MCP 安全；
- [ ] Trace、Benchmark 与数据飞轮。

### 面试表达

- [ ] 核心题均有 20 秒和 2 分钟版本；
- [ ] 通用概念、项目深挖和系统设计三类模板；
- [ ] 三条核心个人标签；
- [ ] 五组以上技术取舍；
- [ ] 不知道问题的诚实回答模板；
- [ ] 10 道高压追问；
- [ ] 现场状态管理检查表；
- [ ] 英文术语均能说明实际作用。

## 六、Harness 允许与禁止的优化

### 允许

- 通用仓库探索和代码定位；
- Context 预算、压缩和证据选择；
- Tool Schema、错误契约和结果限制；
- Patch 生成、应用和回滚可靠性；
- Max Steps、No-progress、Retry 和 Replan；
- Verifier 反馈和测试选择；
- 执行环境、依赖和沙箱兼容性；
- Trace、指标和失败归因。

### 禁止

- 根据 Case ID 走特殊逻辑；
- 将特定 Case 的文件名或答案写入 Prompt；
- 阅读 Gold Patch 后修改策略；
- 向 Agent 暴露 Test Patch 或隐藏测试；
- 删除失败题或替换困难样本；
- 多次运行后只报告最好结果；
- 修改 Harness 后沿用旧版本成绩；
- 将人工修改后的 Patch 计为 Agent Resolved。

## 七、防延期与止损规则

1. Agent、RAG 和 FixLoop 题目补充不做代码实现；单题超过 20 分钟仍无结论时，记录待核验点后继续。
2. 每轮 Harness 只解决影响范围最大的 3 个通用问题，禁止在单个 Case 上消耗半天。
3. 代码定位实验控制在 10～15 个 Case，多模型实验控制在 5～10 个任务，产品对照控制在 2～3 个任务。
4. 单个 Benchmark 环境问题超过 45 分钟，标记环境失败并切换下一题。
5. Verified 50 未稳定前不扩到 100；Verified 100 未完成审计前不启动全量 500。
6. 全量 500 的模型或基础设施预算不足时，保留可信预评测结果，不用不完整全量冒充正式成绩。
7. 8 月 15 日下午冻结 Harness Release Candidate；冻结后只允许修复导致评测无法运行的基础设施故障。
8. 8 月 16 日不新增大型功能，重点是审计、报告和面试表达。

## 八、本周最终交付清单

### Agent

- [ ] 六类新增或补强题集；
- [ ] SWE-bench、LLM Wiki 和知识图谱变体调研；
- [ ] 核心题双版本回答和架构图。

### RAG

- [ ] 六类专项题集；
- [ ] RAG 失败归因树、成本模型和异常诊断树；
- [ ] GraphRAG 评测与个人边界材料。

### FixLoop

- [ ] 代码定位对照报告；
- [ ] 双模型能力矩阵；
- [ ] 主流 Coding Agent 对比报告；
- [ ] Trace 驱动的 Bad Case 数据飞轮；
- [ ] Verified Baseline、Harness 迭代和 RC 对照；
- [ ] Verified 50～100 预评测结果；
- [ ] 预算允许时的 Verified 全量 500 结果；
- [ ] 七类 FixLoop 项目深挖题。

### 面试

- [ ] 20 秒与 2 分钟双版本回答体系；
- [ ] 三类表达模板；
- [ ] 三条个人标签及证据映射；
- [ ] 五组以上技术取舍；
- [ ] 不知道问题的回答模板；
- [ ] 10 道高压追问；
- [ ] 两次完整模拟面试；
- [ ] 现场状态管理检查表。
