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

## 索引：面试常见问题 → ADR

| 问题 | 参见 |
|------|------|
| 为什么不用 LangChain？ | ADR-001 |
| 多 Agent 怎么通信？ | ADR-004 + ARCHITECTURE §5 |
| Token 怎么控？ | ADR-003 |
| 工具怎么防越权？ | ARCHITECTURE §6 + ADR-006 |
| 评测数据可信吗？ | ADR-007 + README 指标表 |
| 怎么调试 Agent？ | ADR-009 |
| 补丁失败怎么办？ | ADR-010 + ARCHITECTURE §5.3 |
