# 2026.08.03—2026.08.09 Agent 求职增强执行计划

## 一、本周目标与执行边界

本周围绕 **三条** 主线推进：

1. **Agent 习题**：LangChain、LangGraph、9 道系统设计题及 15 组后端与基础设施题。  
   **完成标准：生成题库（仅题目）即可**；20 秒/2 分钟口述、架构图、讲稿等成稿由个人另行整理，不纳入本计划验收。
2. **RAG 习题**：EvalScope、Skill Router、安全与生成链路、项目总览与个人边界、Bad Case 五类专项。  
   **完成标准同上：题库即可**；讲稿/卡片/Pipeline 图等成稿自行整理。
3. **FixLoop 实现**：Canonical Trace、Langfuse/Prometheus、GitHub MCP、3 个 Skill 与 Router、工具安全沙箱、SWE-bench Lite 小规模评测及仓库发布化。

> **已移除「面试增强」主线**（自我介绍、JD 匹配表、证据卡、STAR、现场编码训练、短任职定稿、模拟面试等）。相关材料不在本计划跟踪范围。

执行边界：

- Agent 与 RAG 时间块：产出覆盖知识点的题库；不编写框架 Demo；不成稿不阻塞勾选。
- FixLoop 时间块才进行设计、编码、测试和文档修改。
- 每晚可更新完成率与次日输入；8 月 8 日前完成 FixLoop 主功能，8 月 9 日以评测、发布和演示收口为主。

## 二、每日时间安排

- **上午 8:30—12:00**：Agent 习题（题库）或 Benchmark 分析。
- **下午 13:30—18:00**：FixLoop 实现、测试和评测。
- **晚上 19:30—22:30**：RAG 习题（题库）或 FixLoop 批跑/收口。

每个时间块采用：

`明确输入 → 完成核心任务 → 验证结果 → 沉淀题库/代码证据 → 记录未完成项`

## 三、每日执行计划

### 8 月 3 日（周一）

#### 上午｜Agent 习题：LangChain

- [x] 覆盖 Model/Message/Tool/Schema、Agent Loop、Structured Output、Middleware、MCP、流式路由、vs 自研 Runtime 等知识点。

验收物：

- [x] LangChain 题库（仅题目）；→ `docs/interview/2026-08-03-langchain-questions.md`（30 道）

#### 下午｜FixLoop：Canonical Trace 设计与主链路接入

> 实现链路说明：`docs/CANONICAL_TRACE.md`；ADR-011

- [x] 盘点现有 Trace、日志、Callback、状态和评测数据。
- [x] 定义事件信封：`run_id、trace_id、span_id、parent_span_id、event_type、timestamp、status`。
- [x] 定义模型请求、Tool Call、Skill 选择、Context 裁剪与压缩事件。
- [x] 定义 Agent/Task 状态迁移、Token、耗时、重试和终止原因事件。
- [x] 定义 Artifact、测试结果和错误引用。
- [x] 设计 Schema 版本、脱敏、兼容和落库策略。
- [x] 接入一条从 Issue 输入到 Verifier 结束的完整主链路。

验收物：

- [x] Canonical Trace Schema 和 ADR；
- [x] 一条完整示例 Trace；→ `docs/examples/canonical-trace-sample.jsonl`
- [x] Schema 校验、父子 Span、异常闭合测试；
- [x] 可通过 `run_id` 还原执行顺序。

#### 晚上｜RAG 习题：EvalScope

- [x] 离线批跑与指标聚合专项题库（数据集、适配、指标、并发、Manifest、聚合、重跑、报告、职责边界）。

验收物：

- [x] EvalScope 题库（仅题目）；→ `docs/interview/2026-08-03-evalscope-questions.md`（30 道）

---

### 8 月 4 日（周二）

#### 上午｜Agent 习题：LangGraph

- [x] 覆盖 StateGraph、Reducer、并行、Checkpointer、HITL、Subgraph、幂等、vs FixLoop State 等知识点。

验收物：

- [x] LangGraph 题库（仅题目）；→ `docs/interview/2026-08-04-langgraph-questions.md`（30 道）

#### 下午｜FixLoop：Langfuse 与 Prometheus

- [ ] 编写 Canonical Trace 到 Langfuse 的适配层。
- [ ] 在 Langfuse 中查看模型、Tool、Skill、Context 和状态迁移的完整轨迹。
- [ ] 暴露任务成功率、阶段延迟、Token、重试和错误率。
- [ ] Prometheus Label 只使用模型、阶段、Skill、状态和版本等低基数字段。
- [ ] 禁止将 `run_id、user_id、issue_id` 作为 Label。
- [ ] 验证 Metrics 与 Trace 数据口径一致。
- [ ] 验证 Langfuse 或 Prometheus 不可用时不影响主任务。

验收物：

- [ ] 一条可查看的 Langfuse Trace；
- [ ] Prometheus Metrics Endpoint 和基础 Dashboard；
- [ ] 高基数保护、Exporter 失败和脱敏测试。

#### 晚上｜RAG 习题：分级 Skill Router

- [x] 分级链路、离线/线上指标、归因与灰度回滚题库。

验收物：

- [x] Skill Router 题库（仅题目）；→ `docs/interview/2026-08-04-skill-router-questions.md`（30 道）

---

### 8 月 5 日（周三）

#### 上午｜Agent 习题：系统设计第一组

- [x] 企业知识问答 Agent / Coding Agent / Skill Router / Memory / Tool Gateway（高压追问题库）。

验收物：

- [x] 系统设计① 高压追问题库；→ `docs/interview/2026-08-05-system-design-set1-questions.md`（30 道）

#### 下午｜FixLoop：GitHub MCP 最小闭环

> 实现链路说明：`docs/GITHUB_MCP.md`

- [x] 实现 MCP Client 的 `tools/list` 和 `tools/call`。
- [x] 将外部 MCP Tool 转换并注册到 Tool Registry。
- [x] 第一版只开放 Issue/评论、仓库、Commit、Branch、PR 和 Actions 查询。
- [x] 加入角色与权限过滤、参数校验、超时和错误归一化。
- [x] 将 Tool Result 转换为统一 Observation 并写入 Trace。
- [x] Draft PR 作为唯一写操作，执行前必须人工确认。
- [x] 禁止 Merge、删除分支、修改 Secrets 和仓库管理。

验收物：

- [x] `tools/list → Registry → 权限过滤 → tools/call → 错误归一化 → Trace → Observation` 闭环；
- [x] 一次真实只读调用；（Mock 默认；官方 stdio：`GITHUB_PERSONAL_ACCESS_TOKEN` + Docker/自定义 command；`FIXLOOP_GITHUB_MCP_LIVE=1` 可跑 live 测）
- [x] MCP 超时、越权、Schema 错误和服务不可用测试；
- [x] Draft PR 审批演示。

#### 晚上｜RAG 习题：安全与生成链路

- [x] 输入审核、检索可信、生成三阶段、拒答/降级/审计题库。

验收物：

- [x] 安全与生成链路题库（仅题目）；→ `docs/interview/2026-08-05-rag-safety-generation-questions.md`（30 道）

---

### 8 月 6 日（周四）

#### 上午｜Agent 习题：系统设计第二组与后端第一组

- [x] 暂停恢复 / 评测门禁 / 监控降级 / 多租户平台高压追问题库。
- [x] Kafka、Redis、事务/Outbox/Saga、线程池与背压题库（含 Agent 挂钩）。

验收物：

- [x] `docs/interview/2026-08-06-system-design-set2-questions.md`（24 道）
- [x] `docs/interview/2026-08-06-backend-infra-q1-questions.md`（24 道）

#### 下午｜FixLoop：3 个 Skill、Registry 与 Router

- [ ] 实现 `github_issue_ingestion`：Issue 转结构化 IssueSpec。
- [ ] 实现 `stacktrace_localization`：从错误栈定位代码。
- [ ] 实现 `regression_test_selection`：根据 Diff 选择测试并升级验证范围。
- [ ] 每个 Skill 定义 Description、Positive/Negative Trigger、输入输出 Schema。
- [ ] 定义允许 Tool、完成证据、Fallback、版本和生命周期。
- [ ] 完成 Skill Registry。
- [ ] 实现规则、关键词、Embedding 组合路由，LLM 只作为低 Margin 兜底。
- [ ] 准备正例、负例、多 Skill 和相似 Skill 混淆 Case。
- [ ] 将候选分数、选择原因和 Skill 版本写入 Trace。

验收物：

- [ ] 3 个可执行 Skill；
- [ ] Skill Registry 与 Skill Router；
- [ ] 至少 50 条离线路由评测集；
- [ ] Top-1、误触发、漏触发、Fallback、低 Margin 和 Skill 切换指标；
- [ ] 一条完整 Skill 选择 Trace。

#### 晚上｜RAG 习题：项目总览与个人边界

- [x] 华为 Agent RAG 总览/职责边界/深挖追问题库。

验收物：

- [x] `docs/interview/2026-08-06-huawei-rag-boundary-questions.md`（30 道）

---

### 8 月 7 日（周五）

#### 上午｜Agent 习题：后端与基础设施第二、三组

- [x] 第二组：限流熔断、Docker/K8s、MySQL、可观测、SSE/Webhook。
- [x] 第三组：分布式任务、Deadline/取消、配置/Secret、SLO、多实例 Checkpoint、超时对账。

验收物：

- [x] `docs/interview/2026-08-07-backend-infra-q2-q3-questions.md`（33 道）

#### 下午｜FixLoop：工具安全与沙箱执行

- [ ] 每个任务使用独立 Git Worktree，限制文件访问范围。
- [ ] 校验规范化路径、路径穿越、符号链接和越界访问。
- [ ] `read` 限制敏感文件、大文件和二进制文件。
- [ ] `grep` 限制目录、执行时间和返回结果大小。
- [ ] `write` 优先使用 Patch，限制修改范围，高风险文件触发审批。
- [ ] `shell/test` 在 Docker 或受限进程中运行。
- [ ] 限制网络、CPU、内存、进程数和执行时间。
- [ ] 支持取消、清理和失败后的 Worktree 回收。
- [ ] 工具调用、拒绝原因和沙箱违规全部写入 Trace。

验收物：

- [ ] 工具威胁模型与安全能力矩阵；
- [ ] 路径越界、敏感文件、超大结果、恶意命令和任务取消测试；
- [ ] 沙箱违规 Trace；
- [ ] 一次安全执行演示。

#### 晚上｜RAG 习题：Bad Case（含反问/短任职追问题）

- [x] Bad Case 覆盖与飞轮结构追问；业务/技术/面试官反问题；短任职高压追问（均作为题库，不成稿）。

验收物：

- [x] `docs/interview/2026-08-07-badcase-reverse-tenure-questions.md`（30 道）

---

### 8 月 8 日（周六）

#### 上午｜Agent 习题：全量校验

- [x] 校验 LangChain / LangGraph / 9 道系统设计 / 15 组后端覆盖。
- [x] 重复题合并建议、薄弱项与高频抽题表（题目索引；口述定稿不验收）。

验收物：

- [x] `docs/interview/2026-08-08-agent-coverage-checklist.md`

#### 下午｜FixLoop：SWE-bench Lite 适配与开发实例

- [ ] 安装并验证官方 Harness。
- [ ] 准备数据集、Docker 镜像缓存、磁盘和并发资源。
- [ ] 检查模型 API 配额、超时和成本记录。
- [ ] 完成数据加载、仓库准备和 Issue 到 FixLoop 输入的转换。
- [ ] 完成 Agent 执行、Patch 导出和官方 Harness 判分。
- [ ] 先跑通 5 个开发实例。
- [ ] 区分环境失败、Agent 失败和评测失败。
- [ ] 固定 Manifest：Case、模型、Prompt、预算、Tool、代码和 Harness 版本。

验收物：

- [ ] Benchmark Adapter v1；
- [ ] 5 个开发实例的 Manifest、Trace、Patch 和 Harness 报告；
- [ ] 环境、Agent、评测三类失败归因；
- [ ] 可重复运行的评测命令和说明。

#### 晚上｜FixLoop：Lite 批跑

- [ ] 固定 10—30 个 Lite 任务开始批跑。
- [ ] 运行单 Agent 基线和完整 FixLoop。
- [ ] 准备关闭部分 Context 或多 Agent 能力的消融版本。
- [ ] 记录 Resolved Rate、Token、耗时、Steps 和失败类型。

验收物：

- [ ] 第一轮 Lite 批跑结果。

---

### 8 月 9 日（周日）

#### 上午｜FixLoop：SWE-bench Lite 对照、消融与报告

- [ ] 完成固定 Lite 任务的剩余批跑。
- [ ] 完成单 Agent、完整 FixLoop 和消融版本对比。
- [ ] 检查是否存在人工介入、Case 特调或 Manifest 漂移。
- [ ] 汇总 Resolved Rate、环境成功率、Token、耗时、Steps 和失败类型。
- [ ] 形成模型、Context、Tool、Patch、Verifier 和环境失败分布。
- [ ] 明确哪些问题属于通用 Harness，作为 8.10 Verified 迭代输入。

验收物：

- [ ] SWE-bench Lite 小规模可复现评测报告；
- [ ] Baseline、完整版本和消融结果表；
- [ ] 失败分类与下一周 Harness 优先级；
- [ ] 每题完整证据包。

#### 下午｜FixLoop：仓库发布化与项目演示

- [ ] 整理清晰的目录结构与模块边界。
- [ ] 完成 README、架构图和三分钟 Quick Start。
- [ ] 提供 `.env.example`，检查当前文件和历史提交无密钥。
- [ ] 补齐 License、贡献指南和 Issue 模板。
- [ ] 完成 CI、固定依赖和可复现 Docker 环境。
- [ ] 发布 Benchmark 定义、Manifest 和原始结果。
- [ ] 提供一条完整示例 Langfuse Trace（若已接入；否则本地 Canonical Trace 示例）。
- [ ] 准备 `v0.1.0` GitHub Release。
- [ ] 明确“FixLoop 解决什么问题”和“为什么不用普通单 Agent”。
- [ ] 录制 `Issue → Skill 路由 → 修复 → 测试 → Trace → 结果/Draft PR` 演示。

验收物：

- [ ] 可由新环境执行的 Quick Start；
- [ ] 发布检查清单；
- [ ] `v0.1.0` Release Candidate；
- [ ] 项目演示录屏。

#### 晚上｜FixLoop / 习题收口（可选）

- [ ] 核对 §四、§六：习题题库齐全、FixLoop 未完成项记入次周。
- [ ] （可选）个人自行整理口述成稿；**不纳入本计划强制验收**。

---

## 四、内容覆盖核对表

### Agent 习题（题库）

- [x] LangChain 知识点题库
- [x] LangGraph 知识点题库
- [x] 9 道系统设计高压追问题库
- [x] 后端与基础设施 15 组主题题库
- [x] 全量覆盖清单

### RAG 习题（题库）

- [x] EvalScope 专项题库
- [x] 分级 Skill Router 专项题库
- [x] 安全与生成链路专项题库
- [x] 项目总览与个人边界专项题库
- [x] Bad Case / 反问 / 短任职追问题库

### FixLoop 实现

- [x] Canonical Trace
- [ ] Langfuse 与 Prometheus
- [x] GitHub MCP 最小闭环
- [ ] 3 个 Skill、Skill Registry 与 Skill Router
- [ ] SWE-bench Lite 小规模可复现评测
- [ ] 仓库发布化
- [ ] 工具安全与沙箱执行

## 五、防延期与止损规则

1. Agent 与 RAG：只保证题库产出；成稿整理不阻塞计划勾选。
2. 单个 FixLoop 功能阻塞超过 60 分钟，先保留最小闭环和接口，次要能力进入 Backlog。
3. GitHub MCP 真实连接不稳定时，先使用 Mock Server 验证协议、Registry、权限和 Trace。
4. Langfuse 或 Prometheus 不可用时，Canonical Trace 继续落本地，第三方适配不得阻塞主链路。
5. 单个 SWE-bench 环境问题超过 45 分钟，标记基础设施失败并切换下一题。
6. Lite 评测以固定 10—30 题的可复现对照为目标，不追求一周内跑完整个数据集。
7. 仓库发布化优先级为 README、Quick Start、固定环境、Benchmark 证据和演示；装饰性内容可以延后。
8. 每天未完成项不得无条件滚动；只保留影响最终演示或 Benchmark 的任务。

## 六、本周最终交付清单

### Agent / RAG（题库）

- [x] LangChain、LangGraph 专项题库
- [x] 9 道系统设计高压追问题库 + 覆盖清单
- [x] 后端与基础设施专项题库（①②③组）
- [x] RAG 五类专项题库（EvalScope / Skill Router / 安全 / 边界 / Bad Case）
- [ ] （个人另整）口述定稿、架构图、Bad Case 卡片等 — **计划外**

### FixLoop

- [ ] Canonical Trace、Langfuse 和 Prometheus（Canonical Trace 已完成；Langfuse/Prometheus 待功能2）
- [x] GitHub MCP
- [ ] 3 个 Skill、Registry 与 Router
- [ ] 工具安全回归与沙箱演示
- [ ] SWE-bench Lite 对照和消融报告
- [ ] 可发布仓库、Release Candidate 和演示视频

## 七、题库文件一览

| 文件 | 内容 |
|------|------|
| `docs/interview/2026-08-03-langchain-questions.md` | LangChain |
| `docs/interview/2026-08-03-evalscope-questions.md` | EvalScope |
| `docs/interview/2026-08-04-langgraph-questions.md` | LangGraph |
| `docs/interview/2026-08-04-skill-router-questions.md` | Skill Router |
| `docs/interview/2026-08-04-intent-router-questions.md` | FixLoop Intent Router |
| `docs/interview/2026-08-05-system-design-set1-questions.md` | 系统设计① |
| `docs/interview/2026-08-05-rag-safety-generation-questions.md` | 安全与生成 |
| `docs/interview/2026-08-06-system-design-set2-questions.md` | 系统设计② |
| `docs/interview/2026-08-06-backend-infra-q1-questions.md` | 后端① |
| `docs/interview/2026-08-06-huawei-rag-boundary-questions.md` | 项目边界 |
| `docs/interview/2026-08-07-backend-infra-q2-q3-questions.md` | 后端②③ |
| `docs/interview/2026-08-07-badcase-reverse-tenure-questions.md` | Bad Case / 反问 |
| `docs/interview/2026-08-08-agent-coverage-checklist.md` | 全量覆盖清单 |
