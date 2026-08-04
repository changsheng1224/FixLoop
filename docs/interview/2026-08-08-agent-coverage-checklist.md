# Agent 题集全量校验与口述抽题（8.8 上午）

> 对应 `docs/2026-08-03-to-08-09-enhancement-plan.md` 8 月 8 日上午。  
> 本文件是**覆盖清单 + 索引 + 薄弱项 + 高频抽题表**；口述「20 秒 / 2 分钟定稿」仍待你练后填写。

---

## 1. 题库文件索引

| 日期块 | 文件 | 题量 |
|--------|------|------|
| LangChain | `2026-08-03-langchain-questions.md` | 30 |
| LangGraph | `2026-08-04-langgraph-questions.md` | 30 |
| 系统设计① | `2026-08-05-system-design-set1-questions.md` | 30（5 题×6 追问） |
| 系统设计② | `2026-08-06-system-design-set2-questions.md` | 24（4 题×6 追问） |
| 后端① | `2026-08-06-backend-infra-q1-questions.md` | 24 |
| 后端②③ | `2026-08-07-backend-infra-q2-q3-questions.md` | 33 |

（RAG 专项另册，不计入本上午「Agent 习题」校验范围，但高频抽题会串到 Skill Router / 安全。）

---

## 2. LangChain 知识点覆盖

计划口径（8.3 上午 + 周核对「6 类」；题库实际按 8 块展开）：

| 知识点 | 覆盖 | 题号（langchain 文件） | 状态 |
|--------|------|------------------------|------|
| Model / Message / Tool / Schema | ✅ | 1–4 | 够 |
| create_agent / Agent Loop | ✅ | 5–8 | 够 |
| Structured Output | ✅ | 9–11 | 够 |
| Middleware | ✅ | 12–14 | 够 |
| 多 Middleware 顺序与边界 | ✅ | 13–14 | 够（与上合并口述） |
| MCP → Tool | ✅ | 15–17 | 够 |
| 流式与模型路由 | ✅ | 18–20 | 够 |
| vs 自研 Runtime | ✅ | 27–30 | 够 |

**薄弱：** 记忆/上下文（21–23）、生产可观测（24–26）有题，但与 FixLoop 证据绑定弱 → 见 §6。

---

## 3. LangGraph 知识点覆盖

| 知识点 | 覆盖 | 题号（langgraph 文件） | 状态 |
|--------|------|------------------------|------|
| StateGraph / Node / Edge / 条件路由 | ✅ | 1–4 | 够 |
| State Schema / Reducer | ✅ | 5–8 | 够 |
| 并行合并与依赖 | ✅ | 9–12 | 够 |
| Checkpointer / thread_id | ✅ | 13–16 | 够 |
| interrupt / HITL / Resume | ✅ | 17–20 | 够 |
| Subgraph / 多 Agent / 流式 | ✅ | 21–24 | 够 |
| 重放幂等与副作用 | ✅ | 25–28 | 够 |
| vs FixLoop State/Checkpoint | ✅ | 29–30 | 够 |

**薄弱：** time-travel / Command·Send 仅在扩展题中点到，可口述时主动对比 FixLoop checkpoint。

---

## 4. 九道系统设计题索引

| # | 题目 | 追问文件 | 题号 | 架构 | 状态 | 异常 | 安全 | 评测 | 取舍 |
|---|------|----------|------|:----:|:----:|:----:|:----:|:----:|:----:|
| 1 | 企业知识问答 Agent | set1 | 1–6 | △追问逼出 | △ | ✅ | ✅ | ✅ | ✅ |
| 2 | Coding Agent | set1 | 7–12 | △ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 3 | 企业级 Skill Router | set1 | 13–18 | △ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 4 | 长期 Memory | set1 | 19–24 | △ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 5 | Tool Gateway | set1 | 25–30 | △ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 6 | 暂停恢复长任务 Agent | set2 | 1–6 | △ | ✅ | ✅ | △ | △ | ✅ |
| 7 | 评测与发版门禁 | set2 | 7–12 | △ | △ | ✅ | △ | ✅ | ✅ |
| 8 | 全链路监控与降级 | set2 | 13–18 | △ | ✅ | ✅ | △ | ✅ | ✅ |
| 9 | 多租户 Agent 平台 | set2 | 19–24 | △ | ✅ | ✅ | ✅ | △ | ✅ |

说明：当前为**高压追问方案 A**，无独立架构图/讲稿 →「架构」列为 △（需你自己补一张草图再口述）。安全/评测在部分题靠追问覆盖，标 △ 表示建议补 1 句显式取舍。

---

## 5. 后端 15 组主题 → Agent 映射校验

| # | 主题 | 题库 | 题号 | Agent 映射 | 状态 |
|---|------|------|------|------------|------|
| 1 | Kafka 幂等/重试/顺序/积压/DLQ | q1 | 1–6 | 工具回调、评测任务 | ✅ |
| 2 | Redis 一致性/穿透/击穿/锁 | q1 | 7–12 | 配置缓存、Resume 锁 | ✅ |
| 3 | 事务/Outbox/Saga/写幂等 | q1 | 13–18 | run 状态、补偿 | ✅ |
| 4 | 线程池/异步/背压 | q1 | 19–24 | 模型池、批跑隔离 | ✅ |
| 5 | 限流/熔断/降级/重试风暴 | q2q3 | 1–4 | LLM/工具保护 | ✅ |
| 6 | Docker/K8s/发现/灰度/回滚 | q2q3 | 5–8 | Verifier、发布 | ✅ |
| 7 | MySQL 索引/隔离/慢查询 | q2q3 | 9–11 | run 历史查询 | ✅ |
| 8 | 日志/Metrics/Trace 排障 | q2q3 | 12–14 | 失败定位 | ✅ |
| 9 | SSE/WS/Webhook/回调 | q2q3 | 15–17 | 流式与异步工具 | ✅ |
| 10 | 分布式任务/租约/恢复 | q2q3 | 18–20 | 长任务 Worker | ✅ |
| 11 | 连接池/Deadline/取消 | q2q3 | 21–23 | 模型客户端 | ✅ |
| 12 | 配置中心/Secret/多环境 | q2q3 | 24–26 | Prompt/Key | ✅ |
| 13 | 采样/高基数/告警/SLO | q2q3 | 27–29 | 可观测门禁 | ✅ |
| 14 | 多实例 Session/Checkpoint | q2q3 | 30–31 | 水平扩展 | ✅ |
| 15 | 超时状态未知对账 | q2q3 | 32–33 | 幂等重试 | ✅ |

---

## 6. 重复题合并建议（口述时合并，不必删文件）

| 主题 | 重复出处 | 口述合并为一题 |
|------|----------|----------------|
| 规则→Embed→LLM 路由 | Skill Router / Intent / 系统设计③ | 「分级路由 + 低 Margin 澄清 + 不自动入典」 |
| Tool 权限分层 | LangChain 安全 / Gateway 设计 / FixLoop | 「Gateway vs Executor + 默认拒绝」 |
| Checkpoint / Resume / 幂等 | LangGraph / 长任务设计 / 后端租约 | 「状态先写、副作用幂等键、租约防双跑」 |
| Trace vs Metrics vs Log | LangChain 可观测 / 监控设计 / 后端排障 | 「三者分工 + 禁止高基数 label」 |
| 评测门禁 | EvalScope / 发版门禁设计 / Bad Case 飞轮 | 「分层门槛 + Manifest 复跑 + 灰度」 |
| vs LangChain/LangGraph | LangChain§I / LangGraph§H / 面试分析稿 | 统一用「三层差异」答（见 interview-analysis） |

---

## 7. 薄弱项清单（概念有、项目证据弱）

对下列题，口述必须挂 **FixLoop** 或 **华为 RAG** 证据，否则标为「会背不会战」：

| ID | 薄弱点 | 建议关联证据 |
|----|--------|--------------|
| W1 | Middleware 链 | FixLoop：ToolGateway、approval、callback/trace 点 |
| W2 | MCP→Tool 映射 | FixLoop：`docs/GITHUB_MCP.md`（`agent_runtime/mcp/`；Registry + Gateway + Gate7） |
| W3 | LangGraph HITL | FixLoop：Draft PR 人工确认、Gate 审批语义 |
| W4 | 并行 Reducer | FixLoop：多 Agent 共享 run 目录写 report 的冲突规避 |
| W5 | Memory 晋升 | FixLoop：durable memory / promote；华为：会话外知识勿乱写入 |
| W6 | 高基数保护 | FixLoop：`fixloop_*` metrics label 设计 |
| W7 | 超时对账 | FixLoop：工具超时、cancel、run 状态 |
| W8 | GraphRAG Local/Global | 华为：interview-analysis 固定口径 |
| W9 | Citation / 权限纵深 | 华为：检索前滤 + Context 再滤 |
| W10 | 系统设计「架构图」缺失 | 九题各补 1 张手绘再口述（当前仅追问） |

---

## 8. 高频口述抽题表（20 道 · 仅题目）

练习规则：随机抽题，**严格 ≤2 分钟**；先 20 秒结论。定稿栏自行打勾。

| # | 题目（摘要） | 来源 | 20s/2min 定稿 |
|---|--------------|------|:-------------:|
| 1 | Model/Message/Tool/Schema 如何关联 | LC-1 | [ ] |
| 2 | Agent Loop 终止条件谁裁决 | LC-5/7 | [ ] |
| 3 | Middleware 顺序与异常边界 | LC-13 | [ ] |
| 4 | 为何少用/不用 LangChain 做核心 Runtime | LC-27 | [ ] |
| 5 | Reducer 为何不能静默覆盖 | LG-6 | [ ] |
| 6 | interrupt → HITL → Resume 防双副作用 | LG-17–19 | [ ] |
| 7 | LangGraph State vs FixLoop RepairState | LG-30 | [ ] |
| 8 | 企业知识问答：固定多路 vs Agentic | SD1-2 | [ ] |
| 9 | Coding Agent：完成证据 vs Max Steps | SD1-9/分析稿 | [ ] |
| 10 | Skill Router：误触发 vs 漏触发门禁 | SD1-14 | [ ] |
| 11 | Tool Gateway 为何两层 | SD1-25 | [ ] |
| 12 | 长任务 Checkpoint 存什么 | SD2-2 | [ ] |
| 13 | 发版门禁分层硬门槛 | SD2-8 | [ ] |
| 14 | Trace/Metrics/Log 分工；禁止 run_id label | SD2-13 | [ ] |
| 15 | 多租户默认拒绝与审计 | SD2-20/24 | [ ] |
| 16 | Outbox：落库 run + 发评测任务 | BE1-13 | [ ] |
| 17 | 背压：在线 Agent vs 离线批跑 | BE1-21 | [ ] |
| 18 | 重试风暴如何避免 | BE2-3 | [ ] |
| 19 | 多实例 Checkpoint 共享 | BE3-30 | [ ] |
| 20 | 超时后重试如何防重复 PR/扣费 | BE3-33 | [ ] |

---

## 9. 校验结论

| 校验项 | 结果 |
|--------|------|
| LangChain 指定知识点 | ✅ 已覆盖 |
| LangGraph 指定知识点 | ✅ 已覆盖 |
| 9 道系统设计 | ✅ 索引齐全；⚠️ 缺架构图/讲稿正文 |
| 15 组后端 → Agent | ✅ 均有映射题 |
| 重复题 | ✅ 已给合并口述建议 |
| 薄弱项 | ✅ 10 条，需挂项目证据 |
| 高频 20 题 | ✅ 抽题表已出；⚠️ 20s/2min **定稿未写** |

**下一步（需你本地完成）：** 按 §8 随机抽题口述；把定稿写进闪卡或另文；九道系统设计各补一张架构草图。
