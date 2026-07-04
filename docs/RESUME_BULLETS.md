# FixLoop 简历 Bullet 终稿（M8D5）

> 项目：**FixLoop** — 从零构建的 Multi-Agent Python 代码修复系统  
> 数据口径：2026-07-04，`agent_runtime/` + `src/` ~8100 行 Python；**475** pytest；覆盖率 **80%**（见 `docs/CODE_REVIEW.md`）

---

## 中文通用版（5 条，直接粘贴「项目经历」）

**FixLoop | Multi-Agent 代码修复系统 | Python · Agent Runtime · Docker · pytest**

1. 基于 Python 标准库（`urllib`/`subprocess`/`json`/`ast`）**零 LLM 框架依赖**，自研 ~8100 行 Agent 系统：Layer 1 运行时（ReAct 控制循环、tiktoken 上下文预算、三层记忆、7 道工具安全闸口）+ Layer 2 四 Agent 修复流水线（Localizer / Retriever / Patcher / Verifier）。

2. 设计**真 Multi-Agent 分工**：4 个 Agent 为独立运行时实例，经 `ToolGateway` 中间件强制**非重叠 Tool 权限**（Localizer 可 AST 解析不可写文件，Verifier 可 Docker 验证不可改补丁）；Agent 间以 dataclass 结构化状态 + Blackboard 冲突检测协作，区别于「换 System Prompt 即 Multi-Agent」。

3. 自研 **Docker 沙箱 Harness**：单 Turn 单容器（网络隔离 / 资源限额 / tar 传文件绕过 Windows bind mount），补丁**原子应用 + 文件级回滚**；工具层 7 道闸口 + 路径锚定，全链路防路径逃逸与重复调用。

4. 自建 **10 Case 跨类型评测集**（TypeError / ImportError / 逻辑 / 配置 / 复合 × 3 难度），实现 Runner / Single-Agent Baseline / 消融框架与 `regression_check` 回归门禁；正式评测 **60 runs**（full vs single × 10 Case × 3 重复）中 Multi-Agent **30/30 零失败**，Patch 精度 **1.22 vs 0.94**（Single 基线）。

5. 工程质量：**475** 单元测试、**80%** 覆盖率、JSONL Trace + Deterministic Replay、Circuit Breaker API 熔断、GitHub Actions test/eval workflow；配套 `ARCHITECTURE.md`、10 条 ADR 与 3 个可独立运行的 Demo 脚本。

---

## 英文版（5 bullets，外企 / 远程）

**FixLoop | Multi-Agent Code Repair System | Python · Agent Runtime · Docker · pytest**

1. Built an ~8,100-line agent stack from Python stdlib with **zero LLM framework dependencies** (`urllib`, `subprocess`, `json`, `ast`): Layer 1 runtime (ReAct loop, tiktoken context budgeting, 3-layer memory, 7-gate tool safety) plus Layer 2 repair pipeline (Localizer, Retriever, Patcher, Verifier).

2. Designed **genuine multi-agent role separation**: four independent agent instances with **non-overlapping tool permissions** enforced by `ToolGateway` middleware (Localizer parses AST but cannot write; Verifier runs Docker tests but cannot patch). Structured dataclass state and a Blackboard with conflict detection—not prompt renaming.

3. Implemented a **Docker sandbox harness**: one container per verification turn (network isolation, resource limits, tar-based file transfer), **atomic patch apply/rollback**, and 7-stage tool safety gates with workspace path anchoring.

4. Created a **10-case evaluation suite** across error types and difficulties, with Single-Agent baseline, ablation runner, and regression gating. In **60 formal runs** (full vs single × 10 cases × 3 reps), Multi-Agent achieved **30/30 passes** with higher patch precision (**1.22 vs 0.94** baseline).

5. Delivered **475** pytest cases at **80%** coverage, JSONL traces, deterministic replay, API circuit breaker, CI workflows, plus architecture docs, 10 ADRs, and three standalone demo scripts.

---

## 按投递方向微调（替换单条即可）

### AI 应用 / LLM Engineering

替换中文第 3 条为：

> 各 Agent System Prompt 与 JSON 输出格式经评测迭代调优；Orchestrator 按 Issue 类型调度 Localizer∥Retriever→Patcher→Verifier 自愈闭环（feedback 驱动多轮重试），结合 Token 级上下文预算与对话摘要，最大化有效信息密度。

英文对应：replace Bullet 3 with prompt/orchestration/self-healing loop emphasis.

### 基础架构 / 平台开发

强化中文第 1 条末尾：

> 模型客户端用 `urllib` 实现 Anthropic / OpenAI / Ollama 协议适配（重试、SSE、Prompt Cache 透传），无 LangChain 等第三方 Agent 框架。

### 安全 / 质量工程

替换中文第 1 条为：

> 从零构建**安全优先**的代码修复 Agent：5 层防护（路径锚定→审批→配额→容器隔离→ToolGateway 权限）+ Shell 白名单 / 敏感信息脱敏；AST 解析区分代码与注释，降低 Prompt 注入面。

---

## 30 秒电梯演讲（中文）

FixLoop 是我从零写的 Multi-Agent 代码修复系统，没有用 LangChain。底层是自研 Agent 运行时，上层四个 Agent 通过 ToolGateway 持有不同工具权限，在 Docker 里验证补丁。我建了 10 个评测 Case 和消融框架，475 个单测，正式跑下来 Multi-Agent 在 30 次实验里全部修复成功，并且有完整的架构文档和 Trace 可回放。

## 30-second pitch (English)

FixLoop is a multi-agent code repair system I built from scratch without LangChain. It has a custom agent runtime and four role-separated agents with enforced tool permissions via ToolGateway, plus Docker-based verification. I built a 10-case eval suite and ablation framework, 475 unit tests, and full architecture docs with replayable traces.

---

## 使用说明

| 项 | 建议 |
|----|------|
| 条数 | 简历上放 **5 条**，每条 ≤2 行 |
| 关键词 | 确保出现 Python、Multi-Agent、Docker、pytest、AST、安全/Security、CI/CD |
| 数据 | 以 README 评测表为准；Case 为微型 repo，Fix Rate 差距不宜夸大 |
| 链接 | 项目栏附 GitHub：`github.com/changsheng1224/FixLoop` |
| 展开 | 面试细节见 `ARCHITECTURE.md`、`docs/design-decisions.md` |

## 不要写（负面信号）

- 「使用 LangChain 构建 Multi-Agent」
- 「SWE-bench X%」（未跑过）
- 「Fix Rate 100%」作为 headline（应写 30/30 于 60-run 正式评测）
- 超过 6 条 bullet
