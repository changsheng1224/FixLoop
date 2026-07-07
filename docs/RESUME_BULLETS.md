# FixLoop 简历 Bullet

> 项目：**FixLoop** — 从零构建的多智能体 Python 代码自动修复系统  
> **项目行标题（HR 向）：** FixLoop | AI 多智能体代码修复 | Python · Docker · 自动化测试  
> **项目行标题（技术面）：** FixLoop | Multi-Agent 代码修复系统 | Python · Agent Runtime · Docker · pytest

---

## 20 条 Bullet（Bonus 全量完成版 · 推荐）

> **口径说明**：以下 20 条按 `docs/bonus.md` **全部 P1 条目已实现** 撰写，用于简历/面试展示完整能力边界。  
> 基线工程数据仍以 `docs/FINAL_STATS.md` 为准（~9.7k 行 · 476 pytest · 80% 覆盖 · 60-run 正式评测 **30/30**）；Bonus 完成版在基线上扩展 Case 库、Web、可观测与 Context/Memory 深度能力。  
> **勿**将未实际上线的 Web 多租户写成「已商用」；对外表述可用「自研 repair 平台 MVP」。

**1. 零框架双层 Agent 系统**：Python 标准库（`urllib`/`subprocess`/`json`/`ast`）零 LLM 框架依赖，自研 **~10k+ 行**双层架构——Layer 1 ReAct 四阶段循环 + Layer 2 四 Agent 修复流水线；repair 启动 **Agent 池化预热**，复用 prefix hash 与 memory 投影，首轮 latency 显著下降

**2. CancellationToken 全链路取消**：CLI Ctrl+C / Web `POST /cancel` / REPL `/cancel` 统一置位；AgentLoop · ModelClient · ToolExecutor 协作式检查；write/patch **等完回滚**、shell **进程组 SIGTERM**、sandbox **container.kill()** 分策略；L2 Orchestrator **cancel 级联** + `user_cancel` checkpoint 可 `--resume`

**3. Context 工程 L0–L5 分级压缩**：tiktoken 驱动五 section 组装（总预算 ~6000 token）+ **每 section 硬顶**；L1 工具截断 → L2–L4 Snip/Microcompact/Collapse → L5 LLM 摘要；`history.jsonl` **canonical 只追加**；`build()` metadata（sections/cuts/budget）进 trace；Prompt Cache 命中率写入 `report.json`

**4. 四层记忆 + 长短文本分离召回**：Working / Episodic / Durable / Semantic 四层 + 固定 topic Markdown 落盘；**LLM 看全文、Embedding 看短 query**（`derive_embed_query` + head/tail 截断）；修复成功 **repair precedent 写回 Durable**，启动注入 `similar_fixes`；**Memory Dream** 后台去重/GC；租户级路径隔离

**5. 真 Multi-Agent 分工与 ToolGateway**：Localizer / Retriever / Patcher / Verifier **独立 Agent 实例**、各自非重叠 Tool 集合；`ToolGateway` 声明式权限表中间件强制越权拒绝（非 Patcher 不可 write、非 Verifier 不可 sandbox）；**分 Agent 独立 quota**，并行阶段不共享 L1 session 指针

**6. Blackboard + Orchestrator 结构化编排**：Agent 间 **dataclass 协议**交换，不靠自然语言；Blackboard KV **已接入 Orchestrator**（write/read_related/snapshot）；同 key 异 source **冲突检测 + `resolve_conflict` 仲裁**；`RepairState.phase` 与终态 `status` 分离；**阶段级读写锁** + workspace 写窗口单飞

**7. 修复流水线与 YAML Skill 策略**：Orchestrator（纯 Python）驱动 Localizer∥Retriever → Patcher → Verifier；YAML Skill **priority + 最长 pattern** 匹配，策略/示例 patch 注入 Prompt；**分阶段 timeout**；大 Issue **RepairPlan.subtasks** 拆分 + Blackboard 汇总；Retriever LLM 超时 **规则+rg 降级**

**8. 自愈闭环与 Patch 质量闸口**：Docker/pytest 失败 → 结构化 feedback（失败测试 + 上轮 diff + build_log）→ Patcher 重试，**feedback 滑动窗口** + 终态 `fixed|exhausted|regression|timeout|user_cancel`；verify 前 **repo 快照**、失败全量回滚；**AST 语义等价校验**（`semantic_ok|drift`）辅助 patch 审查

**9. JSON 输出多级保障**：Localizer/Patcher 产出经 **Pydantic schema 校验** + strict JSON → json5 → regex 提取多级降级；解析失败附 schema 样例 **自动 parse retry**（≤2 次）；错误进 `agent_errors` / feedback，不拖垮流水线

**10. Docker Harness 四维隔离沙箱**：单 Turn 单容器，tar 传 `/code`（规避 Windows bind mount）、`network_mode=none`、CPU/内存硬限制；**只读 rootfs + tmpfs `/tmp`**、非 root 运行、tar 大小上限；**sandbox 健康探针** + cancel/timeout 统一 kill；逃逸回归 Case（读 `/etc/passwd`/外网/fork）纳入 CI

**11. 九道工具安全闸口纵深**：白名单 → 存在 → validate（路径逃逸）→ quota → duplicate → dry-run → **审批 diff 预览** → 前后快照 diff；`tool_rejections_by_gate` 进 report；全局 `--dry-run` 完整规划后人工确认；写入 ≤20 / Shell ≤10 / 总调用 ≤50 + **context 8000 token 硬顶**

**12. 模型 API 韧性与降级链**：Circuit Breaker（5 失败/30s 半开）+ **熔断事件进 trace**；ModelClient **令牌桶 RPM** + 429 Retry-After jitter；**SSE/chunk 流式** REPL 实时输出；Provider 熔断独立状态；rg→Python grep · Semantic→keyword · Multi-Agent→Single-Agent **最后一搏**降级

**13. Checkpoint 双层断点续跑**：L1 **每 tool 步** checkpoint（`trigger=step_end`）+ freshness hash 三态 resume；L2 **`repair_checkpoint.json`** + `--resume-repair` 阶段续跑；cancel 时写 `user_cancel` checkpoint（含 in-flight tool）；Web 刷新从 Redis 拉 phase **SSE 续跑**

**14. 全链路可观测与 Replay**：L1+L2 共用 **UUID run_id**；`trace.jsonl`（超 1000 行 gzip）+ `report.json`（token/cache/node_timings）+ 结构化 JSON 日志；**Deterministic Replay** 工具行为对比；**Prometheus `/metrics`** + Grafana 面板（node_timings · sandbox_ms）；ReAct 每步 `react_phase` 事件

**15. 自建 20 Case 评测与消融框架**：TypeError/ImportError/AttributeError/logic/config/composite × 多难度；**patch_equivalence_score**（full/partial/none）；`full / single / no_retriever` 消融 + **`--resume` eval 断点** + 并行 `workers=N`；正式评测 **Multi-Agent 30/30 @ 60-run**，Patch 精度 **1.22 vs 0.94**，回归率 **0%**；Pass@k · 分 Agent token/latency 表

**16. Web Repair 平台 MVP**：REST v1（`POST /api/v1/repairs` · SSE events · cancel）；**JWT/租户隔离** + API Key 速率/日 token 配额；每次 repair **workspace jail** + 同 repo **写锁**；Redis 任务队列 + **公平调度 Worker** + cancel 级联；前端实时进度（localize→verify）+ patch diff 高亮

**17. 敏感信息与 Prompt 注入防护**：Shell 环境白名单；**redact 策略表 YAML** + trace/report 脱敏；memory 写入闸口（API key/GitHub token 拒绝）；**prompt 注入对抗 Case** 纳入 eval；AST 注释节点不送 LLM；租户 offboard **trace TTL wipe**

**18. 工具 Schema 工程与 L2 域工具**：`@dataclass` + `auto_schema`/`auto_validate` 唯一真相源；L2 扩展 ast_parse · stack_parse · git_blame/diff · find_test · sandbox_*；**工具 manifest**（`.agent/tools.yaml`）按 Agent 可见性 merge；write_file **原子 replace** · patch **多 hunk 统一 diff**

**19. CLI / REPL 产品化体验**：L1 REPL `/memory` · `/memory forget` · `/sessions` · `/replay` · `/prompt`；**readline 历史** + 多行 `\` 续行；L2 `repair` **退出码规范**（0/1/2/3）；`--health` provider ping · dev/prod/ci profile；REST `--serve` 与 Web 共用 Orchestrator 工厂

**20. 工程质量与容量验证**：**550+** pytest · **85%+** 行覆盖 · Ruff 零 warning；GitHub Actions test/eval workflow + `regression_check` 门禁；**Locust/k6 压测场景库**（API + 沙箱并发）；`ARCHITECTURE.md` · 10 ADR · 3 Demo · `LAYER1/2_GUIDE` 导读文档

### Bonus 版按投递方向优先勾选

| 方向 | 建议编号 |
|------|----------|
| 通用 / 默认 | **1、5、7、8、15** |
| AI 应用 / LLM Engineering | 3、4、7、8、9 |
| 后端 / 平台 / Infra | 2、10、13、14、16 |
| 安全 / 质量工程 | 10、11、17、9、15 |
| 架构 / 系统设计 | 1、5、6、13、18 |

---

## 简历精选 8 条（Bonus 全量完成版 · 可直接粘贴）

> 将 Bonus 版 **#1–#20** 按主题合并为 8 条，覆盖运行时 / 编排 / 沙箱 / 安全 / 评测 / 可观测 / 平台 / 工程；项目经历栏较宽时使用。括号内为合并来源，粘贴时可删。

**1. 零框架双层 Agent 与 Context/Memory 工程（#1 + #3 + #4）**：Python 标准库零 LLM 框架依赖，自研 **~10k+ 行**双层架构；Layer 1 ReAct 四阶段 + **L0–L5 Context**（tiktoken 五 section 硬顶 · canonical `history.jsonl` · Cache 命中率进 report）；Working/Episodic/Durable/Semantic 四层记忆 + **embed_query 与 user 全文分离** + repair precedent 闭环；repair **Agent 池化预热**

**2. 真 Multi-Agent 分工与结构化编排（#5 + #6）**：Localizer / Retriever / Patcher / Verifier **独立实例**、非重叠 Tool 集合；`ToolGateway` 声明式越权拦截 + **分 Agent quota**；Blackboard **接入 Orchestrator**（冲突检测 · `resolve_conflict` · TTL）；`RepairState.phase/status` 分离 + **阶段读写锁**与写窗口单飞

**3. 修复流水线、Skill 策略与 JSON 保障（#7 + #8 + #9）**：Orchestrator（纯 Python）Localizer∥Retriever → Patcher → Verifier；YAML Skill **priority 匹配** + **RepairPlan.subtasks** 大 Issue 拆分；pytest/Docker **feedback 滑动窗口** 自愈重试 + repo 快照回滚 + **AST 语义闸口**；Pydantic schema + 多级 JSON parse 降级

**4. Docker 沙箱隔离与全链路 Cancel（#10 + #2）**：单 Turn 单容器（tar 传 `/code` · 只读 rootfs · `network_mode=none` · 非 root · 资源硬限制）；健康探针 + 逃逸回归 Case；**CancellationToken** 贯穿 AgentLoop/ModelClient/ToolExecutor；write 等完回滚 · shell 进程组 kill · sandbox kill；L2 **cancel 级联** + `user_cancel` checkpoint

**5. 工具链工程与安全闸口纵深（#11 + #18）**：`@dataclass` + `auto_schema`/`auto_validate` + L2 域工具（ast/stack/git/find_test/sandbox）；**工具 manifest** 按 Agent 可见性配置；九道闸口 + **审批 diff 预览** + `tool_rejections_by_gate`；写入/Shell/总调用配额 + **context 8000 token 硬顶** + 全局 `--dry-run`

**6. 模型 API 韧性与敏感信息防护（#12 + #17）**：Circuit Breaker + **熔断事件 trace** · 令牌桶 RPM · SSE 流式输出 · 429 jitter；rg/Semantic/Multi-Agent **多级降级链**；Shell 白名单 + **redact YAML 策略表** + memory 写入闸口；**prompt 注入对抗 Case** 纳入 eval；AST 注释不送 LLM

**7. 20 Case 评测与消融科学（#15 + #20 部分）**：**20 Case** 跨错误类型 × 多难度；`patch_equivalence_score` · full/single/no_retriever 消融 · 并行 eval + **`--resume` 断点** · Pass@k；正式 **60-run Multi-Agent 30/30**，Patch 精度 **1.22 vs 0.94**，回归率 **0%**；`regression_check` CI 门禁

**8. 可观测、Checkpoint、Web MVP 与容量（#13 + #14 + #16 + #19 + #20）**：UUID run_id · gzip trace · Prometheus/Grafana · Deterministic Replay · ReAct phase 事件；L1 **逐步** + L2 **`--resume-repair`** checkpoint；REST v1 + **SSE** repair 平台（租户隔离 · Worker 队列 · workspace jail）；REPL `/memory`·`/replay`；**550+** pytest · **85%+** 覆盖 · Locust/k6 压测

### Bonus 版 8 条 vs 5 条 vs 20 条怎么选

| 场景 | 建议 |
|------|------|
| 简历项目栏（2–3 行/点） | **简明版** 5 条 |
| 与参考项目同风格（分号串联） | **分号串联版** 5 条 |
| 项目正文 / 作品集 / 口述 | **详述版** 5 段 |
| 项目经历栏较宽（6–8 条） | **精选 8 条** |
| 按岗位定制混搭 | 从 **Bonus 20 条** 勾选 |

---

## 简历精选 5 条（Bonus 全量完成版）

> **五条标题**：① 零框架 Agent 运行时 ReAct · ② 上下文 + 记忆工程 · ③ 多 Agent 分工 + 自愈修复编排 · ④ Docker 沙箱 + 任务取消 + 工具安全 · ⑤ 自建评测 + 调用韧性 + 可观测 + 工程化交付  
> **详述版**（下方）：每点一段，适合项目经历正文、个人网站或面试口述展开。  
> **简明版 / 分号串联版**（同节末尾）：适合简历栏位直接粘贴。

---

### 详述版（推荐用于项目正文）

**1. 零框架 Agent 运行时 ReAct**

从零用 Python 标准库搭建约 **1 万行** Agent 系统，不依赖 LangChain 等第三方 LLM 框架：底层是完整的 ReAct 四阶段循环，支持 Function Calling、批量 Tool 调用、单步超时与流式输出；上层四角色修复流水线通过统一工厂装配，推理与编排分层解耦。修复任务启动时 **Agent 预热**，配合 **Prompt Cache** 稳定前缀复用，降低首轮延迟与 Token 成本；支持对话逐步 **Checkpoint** 断点续跑。工具层统一 **Tool Schema** 与参数校验，扩展语法树、堆栈、Git 追溯与测试定位等域 Tool，供各 Agent 在 ReAct 循环中按需调用。

**2. 上下文 + 记忆工程**

为控制长对话与多 Agent 协作成本，实现了分级 Context 管理——精确计数驱动五段拼装（总预算约 6000 token、各段硬顶），历史从规则截断到模型摘要逐级压缩，原始记录只追加不篡改。记忆采用 Working / Episodic / Durable / Semantic 四层分工，长 Issue 全文进 LLM、短 query 进 Embedding 检索，修复成功先例写回 Durable 供后续任务参考，并辅以后台去重与租户级隔离。Orchestrator 将 Issue 与 stack trace 全文钉扎不被压缩。

**3. 多 Agent 分工 + 自愈修复编排**

修复流程由 Localizer / Retriever / Patcher / Verifier 四个独立 Agent 协作——各自绑定非重叠 **Tool** 集合，**ToolGateway** 拦截越权（非换 Prompt 伪 Multi-Agent）。纯 Python **Orchestrator** 调度 localize∥retrieve → patch → verify，**Blackboard** 结构化协作；**YAML Skill** 外置版本化策略，按 Issue 类型注入各角色 System Prompt，复杂 Issue 可拆分子任务。Verifier 在 Docker 内 pytest 判定 Pass / Fail，不通过则结构化 feedback 注入 Patcher 重试，默认最多 **3 轮**，验证前快照、失败回滚；**Repair 阶段 Checkpoint** 支持中断续跑。检索超时或验证耗尽时规则降级，可 Single-Agent 最后一搏。

**4. Docker 沙箱 + 任务取消 + 工具安全**

补丁是否正确不以模型自判为准，而在 Docker 容器内跑 pytest：单 Turn 单容器，代码 tar 传入而非 bind mount，网络关闭、CPU/内存硬限、只读 rootfs、非 root 运行，验证结束即销毁，并配有健康探针与逃逸场景回归。CancellationToken 贯穿 AgentLoop / ModelClient / ToolExecutor 与 L2 Orchestrator：CLI、Web、REPL 统一置位，write/patch **等完回滚**、shell **进程组 SIGTERM**、sandbox **container.kill()** 分策略，cancel 级联并写 `user_cancel` Checkpoint 支持 `--resume`。工具调用串行通过九道闸口——白名单、路径校验、配额与重复检测、演习预览、改码 diff 审批、执行前后快照对比——拒绝次数汇总进 report；Shell 环境白名单、trace/report 密钥脱敏、memory 写入拒绝敏感 pattern，context 总 token 与分角色调用配额设硬顶。

**5. 自建评测 + 调用韧性 + 可观测 + 工程化交付**

自建 **20 个**典型错误 Case 与消融框架（四角色 / Single-Agent / 无 Retriever），Prompt 或 Skill 变更须过 `regression_check` 门禁；正式 **60 次**跑批四角色 **30/30** 全修复，Patch 精度 **1.22 vs 0.94**，**零**引入新失败。运行时 ModelClient 配备 Circuit Breaker、令牌桶限流与 429 jitter，检索 / Semantic / Multi-Agent 链路均有降级路径。每次运行分配 UUID run_id，trace.jsonl + report.json 记录 Token、各 Agent 节点耗时与 Prompt Cache 命中，支持 Deterministic Replay 与 Prometheus/Grafana 面板。Web **repair 平台 MVP** 提供 REST 提交任务、SSE 推送 localize→verify 进度、多租户与 API Key 配额隔离；工程侧 **550+** pytest、**85%+** 覆盖、CI 双流水线、Locust/k6 压测，以及架构文档、十条 ADR、分层导读与三个 Demo。

---

### 简明版（简历栏位紧张时粘贴）

**1. 零框架 Agent 运行时 ReAct（#1 + #13 + #18）**：Python 标准库零 LLM 框架依赖，自研约 **1 万行**双层架构，ReAct 循环 + Function Calling；流式输出、批量 Tool 与步数 / 超时控制；Agent 预热 + Prompt Cache 降本；统一 Tool Schema 与域 Tool（AST / stack / Git / 测试定位）；对话逐步 Checkpoint 断点续跑

**2. 上下文 + 记忆工程（#3 + #4）**：分级 Context 预算（约 6000 token）与历史逐级压缩；Working / Episodic / Durable / Semantic 四层 Memory，长 Issue 全文进 LLM、短 query 进 Embedding，修复先例跨会话复用；Issue / stack trace 钉扎

**3. 多 Agent 分工 + 自愈修复编排（#5 + #6 + #7 + #8 + #9 + #13）**：四角色独立 Tool 集合 + ToolGateway 越权拦截；Orchestrator + Blackboard 编排，YAML Skill 外置策略按 Issue 注入；pytest 失败 → feedback → Patcher 重试（**3 轮**），验证前快照、失败回滚；Repair 阶段 Checkpoint 续跑；链路异常时规则 / Single-Agent 降级

**4. Docker 沙箱 + 任务取消 + 工具安全（#10 + #2 + #11 + #17）**：单 Turn 单容器隔离验证（断网、只读 rootfs、非 root、资源硬限）；CancellationToken 全链路 cancel 级联 + Checkpoint 断点续跑；九道 ToolExecutor 闸口（路径校验、配额、演习预览、改码 diff 审批）；Shell 白名单、trace 脱敏与 memory 写入闸口

**5. 自建评测 + 调用韧性 + 可观测 + 工程化交付（#12 + #13 + #14 + #15 + #16 + #19 + #20）**：**20 Case** 消融 + 正式 **60 次**跑批 **30/30**、Patch **1.22 vs 0.94**、**零**回归；Circuit Breaker + 限流 + 多级降级；UUID run_id、JSON Trace、Deterministic Replay、Prometheus；Web MVP（REST + SSE、多租户队列）；**550+** pytest、**85%+** 覆盖、CI 门禁与压测；架构文档与 Demo

### 分号串联版（对齐参考条目风格 · 可直接粘贴）

> 五条标题按 FixLoop 能力分层；数据口径：**60-run 30/30**、Patch 精度 **1.22 vs 0.94**（见 `FINAL_STATS.md`）。英文保留行业通用词，不写内部缩写。

• **零框架 Agent 运行时 ReAct**：基于 Python 标准库实现无 LLM 框架依赖，自研 Agent 架构；底层 ReAct 循环驱动 Function Calling 与 Tool 执行，上层实现四角色代码修复流水线；支持流式输出、批量调用与步数 / 超时控制；Agent 预热与 Prompt Cache 稳定前缀复用，降低首轮延迟与 Token 成本；统一 Tool Schema 与参数校验，配套代码分析域 Tool

• **上下文 + 记忆工程**：分级 Context 预算（约 6000 token、五段拼装）与历史压缩，从规则截断到模型摘要逐级降级，控制长对话成本并保留关键信息；四层 Memory（Working / Episodic / Durable / Semantic）覆盖近因、任务情节、跨会话规范与语义检索，修复成功先例跨会话复用；长 Issue 全文进 LLM、短 query 进 Embedding，检索与推理输入分离；Orchestrator 将 Issue 与 stack trace 等关键约束钉扎、固定不被裁剪

• **多 Agent 分工 + 自愈修复编排**：Localizer / Retriever / Patcher / Verifier 四角色独立 Tool 集合（非换 Prompt 伪 Multi-Agent），ToolGateway 强制越权拦截；纯 Python Orchestrator 编排「定位检索 → 改码 → 验证」，定位与检索并行，Blackboard 结构化协作；YAML Skill 外置版本化策略，按 Issue 类型注入各角色 System Prompt，复杂任务可拆分子步骤；Docker pytest 作为 ground truth，失败反馈驱动改码重试（默认 **3 轮**），验证前快照、失败回滚；链路异常时规则降级与 Single-Agent 兜底

• **Docker 沙箱 + 任务取消 + 工具安全**：待修代码库打包进隔离容器执行 pytest 验收（断网、资源限额、最小权限，测完即销毁）；CLI / Web 全链路可取消，写操作 / Shell / 沙箱分策略收束，取消后工作区可回滚；双层 Checkpoint 断点续跑（Agent 逐步 + Repair 阶段）；Tool 调用多层安全校验（路径、配额、高风险改码审批）；Shell 白名单、日志脱敏与敏感信息写入防护

• **自建评测 + 调用韧性 + 可观测 + 工程化交付**：**20 Case** 消融验证 Multi-Agent 收益，正式 **60 次**跑批 **30/30**、Patch 精度 **1.22 vs 0.94**、**零**回归；模型 API 熔断限流与多级降级保障可用性；全链路 Trace / 回放与各阶段 Token / 耗时统计；Web repair 平台 MVP（任务提交、实时进度、多租户）；**550+** 自动化测试、**85%+** 覆盖与 CI 评测门禁

### Bonus 版 5 条与 8 条对应关系

| 精选 5 条 | 合并自 Bonus 精选 8 条 |
|-----------|------------------------|
| #1 零框架 Agent 运行时 ReAct | 8 条 #1（ReAct 部分）+ #5（Tool schema）+ #8 部分（Checkpoint L1） |
| #2 上下文 + 记忆工程 | 8 条 #1（Context/Memory 部分） |
| #3 多 Agent 分工 + 自愈修复编排 | 8 条 #2 + #3 + #8 部分（Checkpoint L2） |
| #4 Docker 沙箱 + 任务取消 + 工具安全 | 8 条 #4 + #5 |
| #5 自建评测 + 调用韧性 + 可观测 + 工程化交付 | 8 条 #6 + #7 + #8 |

---

## 面试准备：六类问题索引（对照精选 5 条）

> 按 FixLoop 实际架构划分，**不必强行对标 LangGraph / 纯 RAG 项目**。每类准备 **1 分钟口述 + 3 个追问答** 即可；详述见上文 **精选 5 条 · 详述版** 与 `docs/bonus.md`、`LAYER1_GUIDE.md`、`LAYER2_GUIDE.md`。

### 六类与简历 bullet 对照

| 面试准备类 | 主要对应 bullet | 次要延伸 |
|------------|-----------------|----------|
| **① Agent ReAct 运行时** | 精选 **#1** | #3 各 Agent 内的 ask 循环 |
| **② 多 Agent 编排与自愈闭环** | 精选 **#3** | #1 双层边界 |
| **③ 上下文与记忆工程** | 精选 **#2** | #3 issue/stack 钉扎 |
| **④ 沙箱 / Cancel / 工具安全** | 精选 **#4** | #3 权限网关 |
| **⑤ 评测 / 韧性 / 安全** | 精选 **#5** | #4 闸口与 #3 降级 |
| **⑥ 可观测 / Web / 工程化交付** | 精选 **#5** | #5 CI 门禁 |

**交叉必考题**（各类都可能串到）：为什么不用 LangChain / LangGraph？· 60-run **30/30** 实验怎么设计？· Multi-Agent 比 Single 强在哪（**1.22 vs 0.94**）？· Layer 1 与 Layer 2 边界？

---

### ① Agent ReAct 运行时

**考察什么**：单 Agent 引擎是否「真做过」——循环、停机、工具调用、运行时装配，而非只会调 API。

**FixLoop 落点**：`agent_loop.py` 四阶段；批量工具 / 单步超时 / 流式；`Agent.ask()`；与 L2 的关系（L2 多次 ask，编排在外层）；零框架 stdlib；Agent 预热；dataclass 工具 schema。

**建议准备的问题**

- ReAct 四阶段分别做什么？一 step 里 model 与 tool 的先后关系？
- 停机条件有哪些（final、步数、parse 失败、user_cancel、step_timeout）？
- 为什么自研运行时而不直接用 LangChain？你缺/多了什么？
- Layer 1 和 Layer 2 的边界：谁负责 while 循环，谁负责阶段机？
- 工具调用失败返回结构化结果、不抛异常——为什么这样设计？
- Agent 预热解决什么问题？和 Prompt Cache 如何配合？

**口述锚点**：「底层是完整 ReAct 引擎，上层 repair 只是多次调用 ask + 外层 Python 编排。」

---

### ② 多 Agent 编排与自愈闭环

**考察什么**：是否「真 Multi-Agent」；编排与推理是否分离；闭环 ground truth 是什么。

**FixLoop 落点**：Localizer / Retriever / Patcher / Verifier；ToolGateway；Orchestrator 不调 LLM；RepairState + Blackboard；Localizer∥Retriever 并行；YAML Skill；subtasks；verify → feedback → Patcher ≤3 轮；repo 快照回滚；JSON schema + 多级 parse；AST 语义闸口。

**建议准备的问题**

- 四个 Agent 各做什么、**不能**做什么？和「换四个 prompt」有何本质区别？
- Agent 之间如何传递信息？为什么不用 Agent 互相对话？
- Orchestrator 职责边界？哪些事绝不交给 LLM？
- 自愈闭环的「反思」是什么？和 Reflexion 类方案有何异同？（**pytest 反馈 + 回滚**，非 CoT 自评）
- verify 失败 feedback 里有什么？滑动窗口防什么？
- Blackboard 冲突如何仲裁？phase 与 status 区别？
- Retriever 超时 / verify 连续失败时的降级（规则检索、Single-Agent 最后一搏）？

**口述锚点**：「编排器只调度；对不对由容器内 pytest 判定；失败带结构化 feedback 重试，最多 3 轮。」

---

### ③ 上下文与记忆工程

**考察什么**：Context 预算、压缩、Memory 分层、长文本与检索分离——AI 应用岗高频。

**FixLoop 落点**：五 section ~6000 token、L0–L5；canonical history；Prompt Cache 统计；四层记忆；LLM 全文 vs embedding 短 query；repair precedent / similar_fixes；Memory Dream；L2 issue/stack 钉扎。

**建议准备的问题**

- Context 和 Memory 的区别？为什么 memory 不能当 ground truth？
- 五段 context 各装什么？超预算先裁谁、保护谁（user 永不裁）？
- L1 工具截断 vs L5 历史摘要 vs L2–L4 目标——触发顺序？
- 四层记忆各管什么时间尺度？Durable 为何固定 topic、禁止 LLM 自由建库？
- 用户输入很长，embedding 窗口不够怎么办？
- similar_fixes 如何防污染定位？写入闸口有哪些？
- 和 Claude Code / Cursor Memory 的差异？（同步 hook、可 eval、repair 先例闭环）

**口述锚点**：「存全量、读子集；拼 prompt 是投影，canonical 不动；长 Issue 全文给 LLM，向量只看派生短 query。」

---

### ④ 沙箱 / Cancel / 工具安全

**考察什么**：执行环境安全、工具链可靠、长任务可中断——后端 / 安全 / 质量岗常问。

**FixLoop 落点**：Docker 单 Turn；tar 传码、断网、只读 rootfs、非 root；九道闸口；dry-run / 审批 diff；分角色 quota；CancellationToken 分策略；L1/L2 cancel 级联；checkpoint resume；域工具 ast/stack/git/find_test。

**建议准备的问题**

- 为什么用 Docker 验收？沙箱**保证**什么、**不保证**什么？
- 为什么不 bind mount？Windows 下为何用 tar？
- 九道闸口各防什么？Gate 拒绝为何不抛异常？
- Cancel 时 read / write / shell / sandbox 分别怎么处理？write 为何不能 kill 中途？
- L1 逐步 checkpoint 与 L2 阶段 checkpoint 区别？
- Retriever 是 RAG 吗？和向量库检索项目有何不同？（代码/测试/Git 上下文 + 规则降级）
- context 8000 token 硬顶、写入/Shell/总调用配额的意义？

**口述锚点**：「验证环境隔离 + 调用链九道闸 + 随时可 cancel 且工作区可回滚。」

---

### ⑤ 评测 / 韧性 / 安全

**考察什么**：指标是否可信；系统降级；安全是否可测——别吹「100% 修复率」。

**FixLoop 落点**：20 Case + full/single/no_retriever 消融；60-run **30/30**；patch 精度 **1.22 vs 0.94**；回归率 0%；patch_equivalence；regression_check；熔断 / 限流 / 流式；多级降级链；redact；prompt 注入 eval Case。

**建议准备的问题**

- eval Case 如何设计？fake runner 解决什么？
- full / single / no_retriever 各验证什么假设？
- **30/30** 的口径（Case 数、重复、变体）？为什么不说 Fix Rate 100%？
- 1.22 vs 0.94 如何定义与计算？0% 回归如何测？
- Prompt / Skill 变更如何回归？CI eval workflow 做什么？
- API 熔断、RPM 限流、429 退避解决什么问题？
- 密钥脱敏、memory 写入拒绝、注入对抗 Case 各防什么？

**口述锚点**：「用消融证明分工价值；数字有实验设计；变更走回归门禁。」

---

### ⑥ 可观测 / Web / 工程化交付

**考察什么**：能否上线运维；能否排障；工程成熟度。

**FixLoop 落点**：UUID run_id；trace.jsonl / report.json；gzip；Prometheus；Deterministic Replay；ReAct phase 事件；Web REST+SSE MVP；多租户 / 配额 / 队列 / 写锁；REPL /memory /replay；550+ test、85%+ 覆盖、压测；ADR / Demo / LAYER GUIDE。

**建议准备的问题**

- 一次 run 产出哪些 artifact？多 Agent 如何串 trace？
- Replay 重放什么、不重放什么？和「重跑 LLM」区别？
- 上线后关注哪些指标（latency、token、sandbox 并发、gate 拒绝、fix rate）？
- Web MVP 架构：API、Worker、Redis、SSE 各做什么？与 CLI 共用哪些模块？
- 多租户隔离与 offboard 擦除怎么做？**诚实说 MVP，非商用**
- checkpoint + Web 刷新续跑的用户路径？
- 压测瓶颈通常在 Docker 槽位还是模型 RPM？

**口述锚点**：「trace 可回放、指标可告警、Web 是 repair 平台雏形，工程上有 CI + 文档 + Demo。」

---

### 准备顺序（时间紧时）

| 优先级 | 类 | 原因 |
|--------|-----|------|
| **P0** | ② 多 Agent + 自愈 | 项目核心叙事 |
| **P0** | ⑤ 评测 | 数字必被追问 |
| **P1** | ① ReAct 运行时 | 证明 L1 深度 |
| **P1** | ③ Context/Memory | AI 工程岗高频 |
| **P1** | ④ 沙箱/Cancel/安全 | 后端 / 安全岗 |
| **P2** | ⑥ 可观测/Web/工程 | 平台岗、收尾加分 |

### 2 分钟项目串讲（按六类顺序）

1. **问题**：测试失败类 Issue 自动修复  
2. **①**：自研 ReAct 运行时，零框架 ~1 万行  
3. **②**：四角色 + 编排器，pytest 驱动 3 轮自愈  
4. **③**：分级 Context + 四层记忆控成本  
5. **④**：Docker 验收 + 九道闸口 + 可 cancel  
6. **⑤**：20 Case 消融，60-run **30/30**，精度 **1.22 vs 0.94**  
7. **⑥**：trace/Replay/Web MVP/550+ 测试  

### 不要踩的坑

- 说「用了 LangGraph / LangChain 搭 Multi-Agent」  
- 说「Fix Rate 100%」而非 **30/30 @ 60-run**  
- 把 Web 多租户写成**已商用上线**（应说 **自研 MVP**）  
- 声称沙箱「绝对安全」或能判定业务语义正确  
- 把 Retriever 说成「公司知识库 RAG」——FixLoop 是**代码/测试/Git 上下文检索**

---

## 面试准备：扩展索引（九类 + 卫星题）

> 在 **六类** 基础上，把当前「一题多问、一类过载」的块拆出 **3 个独立类**，并补 **2 类卫星题**（架构边界、并发一致性）。  
> **用法**：时间紧仍用六类；投 AI 工程 / 平台 / 安全岗时，按九类 + 卫星补盲区。

### 为何要从六类扩展？

| 现状问题 | 典型被问到但未单列的内容 |
|----------|--------------------------|
| **② 过载** | Skill/Prompt、JSON 解析、AST 语义闸口、subtasks 与 **② 编排** 混在一类 |
| **④ 过载** | Docker 沙箱、九道闸、Cancel、Checkpoint、Retriever 检索 **四类不同面试官** |
| **⑤ 过载** | 消融指标、熔断限流、脱敏注入 **评测 / SRE / 安全** 混在一起 |
| **⑥ 过载** | trace/Replay 与 Web/多租户/压测 **可观测 vs 产品化** 追问深度不同 |
| **六类缺口** | 「为什么这样设计」ADR、与 LangGraph/Cursor 对比；并行读写锁、同 repo 写冲突 |

### 推荐九类（在六类上拆分/新增）

| 新编号 | 类名 | 从原六类 | 新增/拆分说明 |
|--------|------|----------|---------------|
| **①** | Agent ReAct 运行时 | 原 ① | 略减：预热/Cache 归 ④ |
| **②** | 多 Agent 编排与状态 | 原 ② 前半 | Orchestrator、四角色、ToolGateway、Blackboard、并行、phase/status |
| **③** | 自愈闭环与 Patch 质量 | 原 ② 后半 | verify→feedback、快照回滚、AST 语义、终态枚举 |
| **④** | 上下文与记忆工程 | 原 ③ | 不变 |
| **⑤** | Prompt / Skill / 结构化输出 | **新拆** | 外置 prompt、YAML Skill、JSON 多级 parse、钉扎区 |
| **⑥** | 工具链与代码检索 | 原 ④ 部分 | 九道闸、域工具、Retriever、降级；**不含** Docker |
| **⑦** | 沙箱 / Cancel / Checkpoint | 原 ④ 部分 | Docker 隔离、cancel 分策略、双层断点续跑 |
| **⑧** | 评测与指标科学 | 原 ⑤ 前半 | Case、消融、30/30、1.22 vs 0.94、regression |
| **⑨** | 模型韧性 / 安全合规 / 可观测与 Web | 原 ⑤ 后半 + 原 ⑥ | 可再按岗位拆读（见下） |

**卫星题（不单独成类，但需准备）**

| 卫星 | 何时必问 | 核心问题 |
|------|----------|----------|
| **S1 架构与项目边界** | 架构师 / 深挖设计 | 为何双层、为何不用 LangChain、10 条 ADR 举例、与 Cursor/Claude Code 差异、不做 SWE-bench 的原因 |
| **S2 并发与一致性** | 后端岗 | Localizer∥Retriever 为何安全、读写锁、写窗口单飞、同 repo 多 repair、repo_snapshot 时机 |

---

### ⑤ Prompt / Skill / 结构化输出（新类 · 原②⑤分散）

**FixLoop 落点**：`src/prompts/*.txt`；`src/skills/*.yaml` priority；issue/stack 钉扎；`output_parsers`；Pydantic 校验；parse retry；L2 Orchestrator 手工拼 prompt vs L1 `ContextManager.build()`。

**补充问题（六类中未单列）**

- System Prompt 为何外置文件？按角色拆分的好处？
- Skill 匹配冲突怎么办（priority + 最长 pattern）？无 Skill 命中时行为？
- Localizer/Patcher 为什么强制 JSON 而不是自由文本 patch？
- strict JSON → json5 → regex 降级顺序的设计考虑？
- parse 失败进 feedback 还是 agent_errors？如何不拖垮流水线？
- issue/stack 钉扎区为什么永不裁剪？和 Context budget 冲突吗？
- 如何 eval 一次 Prompt 改动？（固定 Case + regression_check）

---

### ⑥ 工具链与代码检索（从原④拆出）

**FixLoop 落点**：`auto_schema`；ast/stack/git/find_test；ToolGateway；QuotaEnforcer；dry-run；Retriever + `--fast-retrieve`；规则+rg 降级；**不是**企业知识库 RAG。

**补充问题**

- 新增一个工具要改几处？manifest 按 Agent 可见性怎么配？
- Gate 5 语义 duplicate 解决什么？Gate 8/9 快照 diff 与 cancel 回滚关系？
- 审批 diff 预览给 Who 看——CLI 还是 Web？
- Retriever 产出哪些字段？similar_fixes 从哪来、置信度闸口？
- 和「向量库 + 文档 RAG」项目怎么对比你的检索设计？
- rg 不可用、Semantic 挂掉、Retriever LLM 超时三条降级链分别是什么？

---

### ⑦ 沙箱 / Cancel / Checkpoint（从原④拆出）

**FixLoop 落点**：SandboxManager 单 Turn；tar；逃逸 Case；cancel 分策略；L1 step_end checkpoint；L2 repair_checkpoint；Web SSE 续跑。

**补充问题**

- threat model：沙箱防什么、不防什么？为何文档写「不声称绝对安全」？
- verify 用 Docker 还是宿主机 pytest？Eval 与 repair 路径差异？
- cancel 写 checkpoint 含 in-flight tool 含义？resume 从哪一步安全？
- freshness hash 三态（full-valid / partial-stale / workspace-mismatch）各怎么处理？
- 温容器池做不做？为何默认 destroy？（Bonus 权衡题）

---

### ⑧ 评测与指标科学（从原⑤拆出）

**补充问题**

- Case 目录结构（repo + issue + expected_patch）？composite Case 测什么？
- Pass@k、patch_equivalence full/partial/none 解决什么？
- 并行 eval 如何隔离 temp repo？`--resume` 跳过哪些 tuple？
- 负样本 Case（期望 exhausted）要不要建？
- 正式 60-run 为何仍写 10 Case × 3 rep？扩展到 20 Case 后指标怎么报？
- 引入回归率 0% 具体怎么测？（改坏其他 test？）

---

### ⑨ 模型韧性 / 安全合规 / 可观测与 Web（原⑤⑥合并入口，按岗位拆读）

**9a 模型 API 与成本（原⑤）**

- Circuit Breaker 三态与 half-open 抖动？
- 令牌桶 RPM 与 eval 并行 workers 如何共用？
- 流式输出对 UX 与 cancel 的帮助？
- token/cache 进 report 如何驱动成本优化？
- 多 Provider fallback 要不要做？opt-in 原因？

**9b 安全合规（原⑤）**

- 三层 redact（Shell / trace / memory 写入）各防什么？
- prompt 注入写在 issue 里如何测？对抗 Case 通过标准？
- AST 注释不进 LLM 的实现思路？
- 租户 offboard trace TTL wipe？

**9c 可观测与排障（原⑥前半）**

- trace 事件类型有哪些？ReAct phase 如何用？
- Replay 差异定位典型 case？
- Prometheus 看 node_timings vs sandbox_ms 说明什么？
- 多 Agent 同一 repair 如何用 run_id 串联？

**9d Web 与容量（原⑥后半）**

- REST 提交 repair 与 SSE 事件契约？
- workspace jail + 同 repo 写锁解决什么竞态？
- 公平队列 vs 租户 sandbox 槽位？
- Locust/k6 压哪条路径？瓶颈预判？

---

### S1 架构与项目边界（卫星 · 交叉必考）

- 一句话：FixLoop 解决什么问题、**不**解决什么问题？
- Layer 1 / Layer 2 为何分开？能否只用 Single Agent？
- 为什么 Orchestrator 不用 LangGraph？你的状态机图长什么样？
- 选 dataclass 协议而非 Agent 互聊的 trade-off？
- 10 条 ADR 挑 2 条展开（如 ToolGateway、Docker tar、不用 LangChain）
- 和 SWE-bench / Devin / Cursor Agent 的边界与差异？
- 若重做，最先改 architecture 哪一块？

---

### S2 并发与一致性（卫星 · 后端岗）

- Localizer 与 Retriever 并行为何不会写冲突？
- 阶段级读写锁与「仅 Patcher 可写」如何配合 ToolGateway？
- verify 前 snapshot、失败后 restore 与 patch 原子 apply 顺序？
- 同 repo 两个 repair inflight 会发生什么？Web 层如何解决？
- Blackboard 与 RepairState 并发写谁负责？

---

### 九类 vs 六类：怎么用

| 你的目标 | 建议 |
|----------|------|
| 通用 AI 应用岗 | **六类** + **S1** + ⑤ 结构化输出 |
| 后端 / 平台 | 六类 + **⑦** 拆细 + **9d** + **S2** |
| 安全 / 质量 | **⑥⑦** 工具+沙箱 + **9b** + **⑧** |
| 研究型 / 爱问原理 | **S1** + ④ + ⑤ + ⑧ |
| 时间极紧 | 六类 P0/P1 顺序不变，只记每类 **口述锚点** |

### 覆盖率自检（考前勾选）

- [ ] 能画 Layer1 ask 循环 + Layer2 repair 流水线（②③）  
- [ ] 能解释 Context 五段 + Memory 四层（④）  
- [ ] 能讲清九道闸 + Docker 验收 + cancel 策略（⑥⑦）  
- [ ] 能背 30/30 实验设计与 1.22 vs 0.94 含义（⑧）  
- [ ] 能说明为何不用 LangChain + 一个 ADR（S1）  
- [ ] 能口述 trace/Replay/Web MVP 边界（9c/9d）  
- [ ] 能答 Retriever ≠ 企业 RAG（⑥）  
- [ ] 能答 Self-reflection = pytest feedback（③）  

---

## 20 条备选 Bullet（M8 基线 · 已实现能力）

> 数据口径：2026-07-04 · `agent_runtime/` + `src/` **~9,664 行** · **476** pytest · **80%** 覆盖（见 `docs/FINAL_STATS.md`）。  
> 若 Bonus 尚未落地，请用本节而非上方 Bonus 全量版。

**1. 零框架手写 Agent 运行时**：Python 标准库（`urllib`/`subprocess`/`json`/`ast`）构建完整 Agent 运行时，不依赖 LangChain 或任何 LLM 框架；ReAct 循环 Reasoning→Acting→Observation→Recording 四阶段交替直至产出最终答案或步数耗尽；Layer 1 ~4.6k 行 + Layer 2 ~5.1k 行，合计 **~9.7k 行**生产代码

**2. 真多 Agent 分工：不同 Agent 持有不同 Tool 集合**：Localizer 持有 AST 解析和堆栈分析但无权写文件，Retriever 持有搜索和 Git 追溯但无权生成补丁，Patcher 持有读写补丁但无权跑测试，Verifier 持有 Docker 沙箱但无权改代码——不是换 Prompt 名字，是换 Tool 集合

**3. ToolGateway 中间件强制权限隔离**：在 Agent 与 Tool 之间独立部署权限拦截层，Agent 自身无法绕过；授权表声明式定义，非 Verifier 调用沙箱 Tool 直接拒绝，非 Patcher 调用写文件直接拒绝，权限违规对 Agent 透明

**4. Blackboard 共享状态板与冲突检测**：Agent 间不靠自然语言通信，而通过结构化 dataclass 协议读写共享 Blackboard；同 Key 多 Agent 写入触发冲突检测，Orchestrator 仲裁合并；带 TTL 自动过期与 schema 版本兼容检查

**5. YAML Skill 策略系统**：修复策略与代码解耦，YAML 定义触发正则、建议 Tool 管线与示例补丁；Orchestrator 按 Issue 类型自动匹配 Skill 并注入对应 Agent 的 System Prompt；新增错误类型只需添加 YAML 文件

**6. Docker Harness 沙箱执行引擎**：单 Turn 单容器，tar 传文件至 `/code`（规避 Windows bind mount）、`network_mode=none`、CPU/内存硬限制、执行完即销毁；宿主机不安装编译测试工具链；补丁原子化应用，任一失败触发全量文件级回滚；沙箱隔离执行环境，不保证 patch 业务逻辑正确

**7. Verifier 验证与自愈闭环**：Docker 内 `pytest --json-report` 结构化采集失败用例→Orchestrator 提取错误摘要→feedback 注入 Patcher 重写补丁，默认最多 **3 轮**重试直至测试全绿；`VerifyStrategy` 抽象支持宿主机 pytest 与 sandbox 双路径

**8. Context 工程：预算控制、历史压缩与 Prompt Cache**：tiktoken 精确计数驱动五段 Context 拼装（总预算 ~6000 token），超限按关联笔记→历史→记忆→系统提示优先级逐段裁剪；历史智能压缩——最近轮次完整保留，更早轮次合并读文件与工具结果为摘要；长历史超阈值触发模型摘要整段替换旧历史；Prefix 稳定段 Hash 作为 Cache Key，工作区不变时跨轮复用模型缓存

**9. 四层工作记忆 + 本地语义检索**：Working（高频小容量）/Episodic（中频中容量）/Durable（低频持久）/Semantic（语义索引）四层分层；工具执行后自动沉淀摘要、写操作后自动失效过期条目；all-MiniLM-L6-v2 本地 ~80MB 模型做语义检索注入 ContextManager，不依赖外部向量数据库

**10. 工具 Schema 自动生成与参数校验**：工具参数以 `@dataclass` 为唯一真相源，`auto_schema()` 从 type hint 自动推导 schema 字符串，`auto_validate()` 自动生成校验逻辑；新增工具只需定义 dataclass 和执行函数

**11. 九道工具安全闸口**：每次调用依次通过白名单→存在性→参数校验（含路径逃逸）→配额→重复调用检测→Dry-Run→审批→执行前后 SHA256 快照对比；任一闸口失败返回结构化错误而非抛异常

**12. 工具配额与降级链**：`QuotaEnforcer` 硬限制单会话写入 ≤20 次、Shell ≤10 次、总调用 ≤50 次；工具调用内置降级链——rg 不可用自动 fallback Python grep，Docker 不可用时 Orchestrator 降级宿主机 pytest（带 timeout 约束）

**13. Circuit Breaker API 熔断器**：状态机包裹所有模型 API 调用，连续 5 次失败自动断开→30s 后半开探测→成功则恢复；熔断期间立即返回错误不等待超时，保护上游资源

**14. Dry-Run 预览模式**：所有工具支持 `dry_run=True`，不执行实际操作仅返回执行计划；全局 `--dry-run` 开关让 Agent 完整规划修复步骤后由用户审核，高风险操作先预览再确认

**15. Deterministic Replay 行为回放**：从 `trace.jsonl` 读取事件序列，用相同参数重新执行工具并对比结果；差异自动标注，支撑「为什么这次和上次行为不同」的归因排查与回归定位

**16. 消融实验验证真分工价值**：自建 **10 Case** 评测集覆盖 TypeError/ImportError/AttributeError/逻辑/配置/复合 × 多难度；消融框架支持 **full / single / no_retriever** 三组变体；正式 **60 runs**（full vs single × 10 Case × 3 重复）中 Multi-Agent **30/30** vs Single **29/30**，Patch 精度 **1.22 vs 0.94**，引入回归率 **0%**

**17. Prompt 版本化管理与评测回归**：System Prompt 按角色外置 `src/prompts/` 分模板管理；Prompt/Skill 变更经固定评测集与 `regression_check` 回归验证；多维指标含 Fix Rate / Patch Precision / Regression Rate / Token 与耗时

**18. Checkpoint 跨轮恢复与会话续跑**：`Checkpoint` 记录当前目标、卡点、下一步与关键文件 freshness hash；Resume 时自动检测文件新鲜度与 Runtime 身份变化，标记 full-valid / partial-stale / workspace-mismatch 三种状态

**19. 三层敏感信息过滤与 Prompt 注入防护**：L1 Shell 环境变量白名单仅透传安全变量；L2 API Key/Token 正则脱敏为 `<redacted>` 写入 trace/report；L3 secret 字段与 `.env` 类路径不入 Trace；AST 解析区分代码节点与注释节点，注释内容不送 LLM

**20. 全链路可观测与 CI 回归门禁**：`trace.jsonl` 逐事件追加 + `report.json` 结构化摘要 + `TaskState` 状态机贯穿全链路；Pydantic 启动配置校验；进度回调流式输出；**476** pytest、**80%** 覆盖率、Ruff 零 warning；GitHub Actions 自动测试 + 评测回归门禁

> **可选替换 #20**：Web 多用户产品化方案（REST/SSE、租户隔离、API+Worker 队列，见 `docs/bonus.md` §IV.8）——简历若写须标注 **设计中 / Roadmap**，勿写成已上线。

### 按投递方向优先勾选

| 方向 | 建议编号 |
|------|----------|
| 通用 / 默认 | **1、2、3、6、16**（或 **20** 强调工程质量） |
| AI 应用 / LLM Engineering | 2、5、7、8、16 |
| 后端 / 平台 / Infra | 1、6、13、15、20 |
| 安全 / 质量工程 | 3、6、11、19、20 |
| 架构 / 系统设计 | 1、3、4、10、18 |

---

## 简历精选 8 条（M8 基线 · 20 条合并版）

> 将备选 **#1–#20** 按主题合并为 8 条，覆盖架构 / 编排 / 沙箱 / 运行时 / 安全 / 评测 / 可观测 / 工程质量；简历项目经历栏可整段使用（比 5 条推荐组合更完整，比 20 条备选更紧凑）。括号内标注合并来源，粘贴时可删。

**1. 零框架双层 Agent 系统（#1 + #8 + #9）**：Python 标准库零 LLM 框架依赖，自研 ~**9.7k 行**双层架构——Layer 1 ReAct 四阶段循环、tiktoken ~6000 token 五段 Context 预算与历史压缩、Working/Episodic/Durable/Semantic 四层记忆 + 本地语义检索；Layer 2 四 Agent 修复流水线

**2. 真 Multi-Agent 分工与权限隔离（#2 + #3 + #4）**：Localizer / Retriever / Patcher / Verifier 独立实例、各自非重叠 Tool 集合；`ToolGateway` 中间件强制越权拒绝；Blackboard 结构化 dataclass 协作 + 冲突检测与 TTL，非换 Prompt 伪 Multi-Agent

**3. 修复编排与策略注入（#5 + #7）**：Orchestrator 驱动 Localizer∥Retriever → Patcher → Verifier 流水线；YAML Skill 按 Issue 类型匹配策略并注入 Prompt；Docker `pytest --json-report` 失败结构化反馈 Patcher，最多 **3 轮**自愈重试

**4. Docker 沙箱 Harness（#6）**：单 Turn 单容器，tar 传 `/code`、`network_mode=none`、CPU/内存硬限制、执行完即销毁；补丁原子应用 + 文件级回滚；宿主机无需安装测试工具链

**5. 工具链工程与安全闸口（#10 + #11 + #12 + #14）**：`@dataclass` + `auto_schema`/`auto_validate` 自动生成工具 Schema；九道闸口（白名单、参数校验、配额、重复检测、Dry-Run、审批、快照 diff）；写入 ≤20 / Shell ≤10 / 总调用 ≤50 硬配额；rg→Python grep 降级链

**6. 模型调用韧性与敏感信息防护（#13 + #19）**：Circuit Breaker 连续 5 次失败熔断、30s 半开探测；Shell 环境白名单 + trace/report API Key 脱敏 + secret 路径隔离；AST 区分代码与注释，注释不送 LLM

**7. 自建评测与消融验证（#16 + #17）**：**10 Case** 跨 TypeError/ImportError/逻辑/配置/复合 × 多难度；full / single / no_retriever 消融 + `regression_check`；正式 **60 runs** 中 Multi-Agent **30/30** vs Single **29/30**，Patch 精度 **1.22 vs 0.94**，回归率 **0%**

**8. 可观测、回放与工程质量（#15 + #18 + #20）**：`trace.jsonl` + `report.json` + `TaskState` 全链路追踪；Deterministic Replay 行为对比；Checkpoint 跨轮恢复（full-valid / partial-stale / workspace-mismatch）；**476** pytest、**80%** 覆盖率、GitHub Actions CI

### M8 基线 8 条 vs 5 条 vs 20 条怎么选

| 场景 | 建议 |
|------|------|
| Bonus 全量完成 | 用文首 **Bonus 精选 8/5 条** |
| 仅 M8 已实现 | 用下方 **M8 精选 8/5 条** |
| 按岗位定制混搭 | 从对应版本 **20 条** 勾选 |

---

## 简历精选 5 条（M8 基线 · 20 条合并版）

> 将备选 **#1–#20** 进一步压缩为 **5 条**，适合绝大多数简历项目经历栏（每条控制在 1–2 行）。括号内标注合并来源，粘贴时可删。

**1. 零框架双层 Agent 运行时（#1 + #8 + #9 + #10）**：Python 标准库（`urllib`/`subprocess`/`json`/`ast`）零 LLM 框架依赖，自研 ~**9.7k 行**双层架构；ReAct 四阶段循环、tiktoken ~6000 token Context 预算与历史压缩、四层记忆 + 本地语义检索；`@dataclass` + `auto_schema`/`auto_validate` 工具 Schema 自动生成

**2. 真 Multi-Agent 分工与修复编排（#2 + #3 + #4 + #5 + #7）**：Localizer / Retriever / Patcher / Verifier 独立实例、各自非重叠 Tool 集合；`ToolGateway` 越权拦截 + Blackboard 结构化协作；YAML Skill 按 Issue 注入策略；Orchestrator 驱动修复流水线，pytest 失败 feedback Patcher 最多 **3 轮**自愈重试

**3. Docker 沙箱与工具安全纵深（#6 + #11 + #12 + #13 + #19）**：单 Turn 单容器（tar 传 `/code`、`network_mode=none`、资源硬限制），补丁原子应用 + 文件级回滚；九道工具闸口 + 写入/Shell/总调用配额；Circuit Breaker 熔断；Shell 白名单 + trace 脱敏 + AST 注释隔离

**4. 自建评测与消融验证（#16 + #17）**：**10 Case** 跨 TypeError/ImportError/逻辑/配置/复合 × 多难度；full / single / no_retriever 消融 + `regression_check`；正式 **60 runs** 中 Multi-Agent **30/30** vs Single **29/30**，Patch 精度 **1.22 vs 0.94**，回归率 **0%**

**5. 可观测、回放与工程质量（#14 + #15 + #18 + #20）**：`trace.jsonl` + `report.json` + Deterministic Replay + Checkpoint 跨轮恢复；全局 `--dry-run` 预览；**476** pytest、**80%** 覆盖率、Ruff 零 warning；GitHub Actions test/eval workflow；配套 `ARCHITECTURE.md`、10 条 ADR 与 3 个 Demo

### M8 基线 5 条与 8 条对应关系

| 精选 5 条 | 合并自精选 8 条 |
|-----------|-----------------|
| #1 | 8 条 #1 + 部分 #5 |
| #2 | 8 条 #2 + #3 |
| #3 | 8 条 #4 + #5 + #6 |
| #4 | 8 条 #7 |
| #5 | 8 条 #8 |

---

## 推荐组合（同「精选 5 条」，历史别名）

内容与上方 **简历精选 5 条** 一致，保留此标题便于旧链接跳转。直接粘贴请用 **精选 5 条** 区块。

---

## 英文版（Bonus 全量完成 · 5 bullets · 简历用）

**FixLoop | Multi-Agent Code Repair System | Python · Docker · pytest**

1. **Zero-framework Agent runtime (ReAct)**: Built a **~10k-line** dual-layer stack in Python stdlib with **no LLM framework dependencies**—custom ReAct loop with Function Calling and tool execution; streaming, batch tools, step/time limits; agent warm-up and **Prompt Cache** prefix reuse; unified **Tool Schema** with domain tools (AST, stack, Git, test discovery); **step-level Checkpoint** resume.

2. **Context + memory engineering**: Tiered **~6000-token** context budget with five-section assembly and tiered history compression; Working / Episodic / Durable / Semantic memory layers; full Issue text to LLM vs short query to embeddings; repair precedent reuse across sessions; Orchestrator pins Issue and stack trace from truncation.

3. **Multi-agent repair + self-healing orchestration**: Localizer / Retriever / Patcher / Verifier with **non-overlapping tool sets** and `ToolGateway` enforcement; pure-Python Orchestrator + Blackboard; versioned **YAML Skills** injected per Issue type; pytest-driven feedback loop (up to **3 rounds**) with snapshot rollback; **repair-stage Checkpoint** resume; degradation to rules or single-agent fallback.

4. **Docker sandbox + cancellation + tool security**: One container per verification turn (network-off, read-only rootfs, non-root, resource limits); **CancellationToken** with cancel cascade and checkpoint resume; nine-stage tool safety gates (path checks, quotas, dry-run preview, patch diff approval); shell whitelist and trace redaction.

5. **Eval + API resilience + observability + delivery**: **20-case** ablation suite—**30/30** in 60 formal runs, patch precision **1.22 vs 0.94**, **0%** regressions; circuit breaker and rate limiting with degradation chains; JSON trace, deterministic replay, Prometheus; **Web repair MVP** (REST + SSE); **550+** tests at **85%+** coverage, CI gates, and load-test scenarios.

---

## 英文版（M8 基线 · 5 bullets，外企 / 远程）

**FixLoop | Multi-Agent Code Repair System | Python · Agent Runtime · Docker · pytest**

1. Built an **~9.7k-line** agent stack from Python stdlib with **zero LLM framework dependencies** (`urllib`, `subprocess`, `json`, `ast`): Layer 1 runtime (ReAct loop, tiktoken context budgeting, 4-layer memory, tool safety gates) plus Layer 2 repair pipeline (Localizer, Retriever, Patcher, Verifier).

2. Designed **genuine multi-agent role separation**: four independent agent instances with **non-overlapping tool permissions** enforced by `ToolGateway` middleware (Localizer parses AST but cannot write; Verifier runs Docker tests but cannot patch). Structured dataclass state and a Blackboard with conflict detection—not prompt renaming.

3. Implemented a **Docker sandbox harness**: one container per verification turn (filesystem / network / resource / privilege isolation, tar-based file transfer), **atomic patch apply/rollback**, and multi-stage tool safety gates with workspace path anchoring.

4. Created a **10-case evaluation suite** across error types and difficulties, with Single-Agent baseline, ablation runner, and regression gating. In **60 formal runs** (full vs single × 10 cases × 3 reps), Multi-Agent achieved **30/30 passes** with higher patch precision (**1.22 vs 0.94** baseline).

5. Delivered **476** pytest cases at **80%** coverage, JSONL traces, deterministic replay, API circuit breaker, CI workflows, plus architecture docs, 10 ADRs, and three standalone demo scripts.

---

## 单条替换示例（从 20 条池中换入）

| 场景 | 用 # 替换精选 5 条中的某条 |
|------|---------------------------|
| 强调 Context / Token 工程 | 用 **#8** 替换精选 5 条第 1 条 |
| 强调编排与自愈闭环 | 用 **#7** 替换精选 5 条第 2 条 |
| 强调安全纵深 | 用 **#19** 单独展开，替换精选 5 条第 3 条 |
| 强调 L1 运行时深度 | 用 **#13** 或 **#15** 替换精选 5 条第 5 条 |
| 强调 Web 规划（面试 Roadmap） | 用可选 **#20 替换项** 替换精选 5 条第 5 条（勿写「已上线」） |

---

## 30 秒电梯演讲（中文）

FixLoop 是我从零写的 Multi-Agent 代码修复系统，没有用 LangChain。底层是自研 Agent 运行时，上层四个 Agent 通过 ToolGateway 持有不同工具权限，在 Docker 里验证补丁。我建了 10 个评测 Case 和消融框架，476 个单测，正式跑下来 Multi-Agent 在 60-run 评测里 full 模式 30/30 全部修复成功，并且有完整的架构文档和 Trace 可回放。

## 30-second pitch (English)

FixLoop is a multi-agent code repair system I built from scratch without LangChain. It has a custom agent runtime and four role-separated agents with enforced tool permissions via ToolGateway, plus Docker-based verification. I built a 10-case eval suite and ablation framework, 476 unit tests, and full architecture docs with replayable traces. Multi-Agent achieved 30/30 passes in our 60-run formal evaluation.

---

## 使用说明

| 项 | 建议 |
|----|------|
| 条数 | 简历标准 **5 条**；空间允许用 **8 条**；每条 ≤2 行 |
| HR 初筛 | 用 **Bonus 精选 5 条（面向 HR）** |
| 研发初筛 / 技术面 | 用 **Bonus 精选 8 条** 或 **技术面 5 条** |

| 版本 | 20 / 8 / 5 区块位置 |
|------|---------------------|
| **Bonus 全量完成** | 文首 Bonus 20 条 · 精选 8 条 · 精选 5 条 · Bonus 英文 5 条 |
| **M8 基线** | 「M8 基线 20 条」·「M8 精选 8/5 条」· M8 英文 5 条 |

| 项 | 建议 |
|----|------|
| 关键词 | Python、Multi-Agent、Docker、pytest、AST、Context Engineering、CI/CD |
| 数据 | 评测以 `docs/FINAL_STATS.md` 为准（**30/30 @ 60-run**）；Bonus 版 Case 20 / 550+ test 为完成态口径 |
| 链接 | 项目栏附 GitHub：`github.com/changsheng1224/FixLoop` |
| 展开 | 面试六类索引见本文 **「面试准备：六类问题索引」**；设计见 `docs/bonus.md`、`LAYER1/2_GUIDE.md` |

## 不要写（负面信号）

- 「使用 LangChain 构建 Multi-Agent」
- 「SWE-bench X%」（未跑过）
- 「Fix Rate 100%」等未在 `FINAL_STATS.md` 口径内的 headline（应写 **30/30 @ 60-run 正式评测**）
- **Bonus 版**：将 Web 多租户写成**已商用上线产品**（应写 **自研 MVP / 内部平台**）
- **M8 基线版**：将 Web 多用户（`bonus.md` §23）写成已上线
- 超过 **8 条** bullet（除非岗位明确要求项目细节极多）
