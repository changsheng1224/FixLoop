# FixLoop 架构文档

> Layer 1 手写 Agent 运行时 + Layer 2 多 Agent 修复流水线。  
> 快速上手见 [README.md](README.md)；设计取舍见 [docs/design-decisions.md](docs/design-decisions.md)。

## 目录

- [1. Layer 1：Agent 运行时模块](#1-layer-1agent-运行时模块)
- [2. Layer 1 调用时序](#2-layer-1-调用时序)
- [3. Layer 1 数据流](#3-layer-1-数据流)
- [4. Layer 2：多 Agent 协作](#4-layer-2多-agent-协作)
- [5. RepairState 状态变换](#5-repairstate-状态变换)
- [6. 安全模型](#6-安全模型)

---

## 1. Layer 1：Agent 运行时模块

Layer 1 位于 `agent_runtime/`，约 1900 行核心代码。对外唯一入口是 `Agent.ask()`。

### 1.1 入口与配置

| 模块 | 职责 | 输入 | 输出 | 为何存在 |
|------|------|------|------|----------|
| `cli.py` / `__main__.py` | CLI 装配与 REPL | 命令行参数、用户 prompt | Agent 实例、退出码 | 把 Config / Workspace / Client 一次性接好，降低使用门槛 |
| `config.py` | `AgentConfig`（pydantic） | provider、max_steps、approval 等 | 校验后的配置对象 | 集中约束运行时参数，避免魔法字符串散落 |
| `workspace.py` | `WorkspaceContext` | cwd 路径 | git 信息 + 白名单文档 + 指纹 | 给模型稳定的仓库上下文，不依赖 LLM 自己 `git status` |

### 1.2 Agent 核心

| 模块 | 职责 | 输入 | 输出 | 为何存在 |
|------|------|------|------|----------|
| `runtime.py` | `Agent` 类 | model_client、tools、config | `ask()` / `parse()` / `execute_tool()` | 对外唯一 façade，隐藏循环与记忆细节 |
| `agent_loop.py` | 控制循环 | user prompt、session | final 文本、trace、node_timings | ReAct 循环的唯一实现；停机条件、retry 退避在此 |
| `context_manager.py` | Token 预算与历史压缩 | session.history、memory | 裁剪后的 messages | 防止上下文爆炸；超预算时摘要而非截断丢弃 |
| `prompt_prefix.py` | System Prompt 组装 | config、dry_run、approval | 完整 system 前缀 | 动态注入规则（dry-run / 审批策略）而不改 Agent 代码 |

### 1.3 模型后端

| 模块 | 职责 | 输入 | 输出 | 为何存在 |
|------|------|------|------|----------|
| `providers/clients.py` | 多 Provider HTTP 客户端 | prompt、max_tokens | 模型文本 + latency | Fake / Anthropic 兼容 / OpenAI / Ollama 同接口，可替换 |
| `providers/circuit_breaker.py` | API 熔断 | 连续失败计数 | 允许/拒绝请求 | 避免 API 故障时无限重试烧配额 |

### 1.4 工具系统

| 模块 | 职责 | 输入 | 输出 | 为何存在 |
|------|------|------|------|----------|
| `tools.py` | 6 个基础工具实现 | 路径、搜索词等 | 文件内容 / 搜索结果 | 读写与搜索是 Agent 与代码库交互的最小集合 |
| `schema_utils.py` | 参数 schema 推导 | 函数 type hints | JSON schema + 校验 | 零手写 schema，工具签名即契约 |
| `tool_context.py` | 路径解析与逃逸检测 | 相对路径 | 绝对路径或拒绝 | **路径锚定**：禁止 `../` 逃出 workspace |
| `tool_executor.py` | 9 道执行闸口 | tool_name、args | `ToolExecutionResult` | 所有工具必经安全检查；失败不抛异常，模型可读错误 |

### 1.5 记忆与持久化

| 模块 | 职责 | 输入 | 输出 | 为何存在 |
|------|------|------|------|----------|
| `features/memory/` | 四层记忆 | 工具结果、用户输入 | working / episodic / durable / semantic | 短程任务态 + 长程笔记 + 向量检索，分层控制成本 |
| `task_state.py` | 任务状态机 | goal、blocker | status、node_timings | 结构化记录「进行到哪一步」 |
| `session_store.py` | 会话 JSON 持久化 | session 对象 | `.agent/sessions/` 文件 | 支持 `--resume latest` |
| `run_store.py` | 单次 run 工件 | trace、report | `task_state.json` + `trace.jsonl` | 可审计、可回放 |
| `checkpoint.py` | 跨轮恢复检查点 | workspace 指纹 | full-valid / partial-stale 等 | 离线改文件后 resume 不 silently 错 |

### 1.6 安全与辅助

| 模块 | 职责 | 输入 | 输出 | 为何存在 |
|------|------|------|------|----------|
| `security.py` | 敏感信息脱敏 | 文本、环境变量 | redacted 字符串 | API key / token 不进 trace 与 report |
| `callbacks.py` | 进度回调 | loop 事件 | 终端彩色输出 | REPL 与 `--verbose` 的可观测性 |
| `replay.py` | Trace 回放 | trace.jsonl | 重放工具执行 | 调试与回归，无需再调 API |

---

## 2. Layer 1 调用时序

一次 `python -m agent_runtime "..."` 的完整路径：

```
用户输入
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ cli.main()                                                    │
│   _load_dotenv() → _make_config() → WorkspaceContext.build()  │
│   _build_model_client() → Agent(...)                          │
└───────────────────────────────┬───────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│ Agent.ask(prompt)                                             │
│   session 初始化 → checkpoint 评估 → memory hooks             │
└───────────────────────────────┬───────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│ AgentLoop.run(prompt)                          [while 循环]   │
│                                                               │
│   ① ContextManager.build_messages()                           │
│        └─ prompt_prefix + workspace + memory + history       │
│                                                               │
│   ② circuit_breaker.call(client.complete)                     │
│        └─ HTTP → 模型返回 raw text                            │
│                                                               │
│   ③ Agent.parse(raw)                                          │
│        ├─ <tool name="..." args='...'>  → 工具调用            │
│        ├─ <final>...</final>            → 结束循环            │
│        └─ 解析失败                      → retry 提示            │
│                                                               │
│   ④ ToolExecutor.execute(name, args)   [9 道闸口]            │
│        └─ tools.* 实际执行 → ToolExecutionResult              │
│                                                               │
│   ⑤ session.history.append + update_memory()                  │
│        └─ trace.jsonl 追加事件                                │
│                                                               │
│   ⑥ 达到 max_steps 或收到 <final> → 退出                    │
└───────────────────────────────┬───────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│ 收尾                                                          │
│   run_store 写 report.json → session_store 原子保存           │
│   返回 final 文本给用户                                       │
└───────────────────────────────────────────────────────────────┘
```

**关键 invariant：** 模型永远看不到「未经过闸口」的工具副作用；dry-run 在第 6 道闸口短路，只返回计划文本。

---

## 3. Layer 1 数据流

```
                    ┌─────────────┐
                    │ User Prompt │
                    └──────┬──────┘
                           │
         ┌─────────────────▼─────────────────┐
         │         ContextManager            │
         │  ┌─────────┬─────────┬─────────┐  │
         │  │ System  │ Memory  │ History │  │
         │  │ Prefix  │ Layers  │ (trim)  │  │
         │  └─────────┴─────────┴─────────┘  │
         └─────────────────┬─────────────────┘
                           │ messages[]
                           ▼
                    ┌─────────────┐
                    │ ModelClient │◄── prompt_cache_key (可选)
                    └──────┬──────┘
                           │ raw text
                           ▼
                    ┌─────────────┐
                    │ Agent.parse │
                    └───┬─────┬───┘
                        │     │
               tool     │     │ final
                        ▼     ▼
              ┌─────────────┐  返回用户
              │ToolExecutor │
              │ 9 gates     │
              └──────┬──────┘
                     │ ToolExecutionResult
         ┌───────────┼───────────┐
         ▼           ▼           ▼
   session.history  memory   trace.jsonl
         │                       │
         └───────────┬───────────┘
                     ▼
              下一轮 loop 输入
```

| 数据 | 存储位置 | 生命周期 |
|------|----------|----------|
| 对话历史 | `session.history` | 单次 session；可 resume |
| 工具 trace | `.agent/runs/*/trace.jsonl` | 追加写，不可变 |
| 工作记忆 | `session.memory["working"]` | 当前 task |
| 持久笔记 | `.agent/memory/*.md` | 跨 session |
| 语义向量 | `.agent/semantic/` | 跨 session，可重建 |

---

## 4. Layer 2：多 Agent 协作

Layer 2 位于 `src/`，在 Layer 1 之上实现分工修复。**Orchestrator 不调 LLM**，只做调度。

### 4.1 协作总览

```
Issue 文本
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ Orchestrator (纯 Python)                                    │
│   _parse_issue()  → RepairPlan                              │
│   _match_skill()  → suggested_tools (YAML)                  │
└───────────────────────────┬─────────────────────────────────┘
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
   ┌─────────────────┐             ┌─────────────────┐
   │   Localizer     │             │   Retriever     │
   │ ast/stack/git   │             │ search/find_test│
   │ → SuspectLocation[]           │ → RetrievedContext
   └────────┬────────┘             └────────┬────────┘
            │         并行 (ThreadPool)    │
            └───────────────┬───────────────┘
                            ▼
                   RepairState 填充
                            │
                            ▼
            ┌───────────────────────────────┐
            │ Patcher (串行, 可重试)         │
            │ read → JSON patch → write_file │
            │ → CandidatePatch[]           │
            └───────────────┬───────────────┘
                            ▼
            ┌───────────────────────────────┐
            │ Verifier                      │
            │ pytest (本地) / Docker (沙箱)  │
            │ → VerificationResult          │
            └───────────────┬───────────────┘
                            │
              ┌─────────────┴─────────────┐
              │ all_passed                │ failed
              ▼                           ▼
         status=fixed              回滚 repo 快照
                                   feedback → retry Patcher
                                   (max_retries 次)
```

### 4.2 各 Agent 职责

| Agent | 工具子集 | 产出 | max_steps |
|-------|----------|------|-----------|
| **Localizer** | ast_parse, stack_parse, git_blame, read/search | `SuspectLocation[]` | 6 |
| **Retriever** | search, find_test, git_diff | `RetrievedContext` | 6 |
| **Patcher** | read_file, write_file, patch_file | `CandidatePatch[]` | 8 |
| **Verifier** | sandbox_build, sandbox_test（或本地 pytest） | `VerificationResult` | — |

每个 Agent 是 **独立的 `Agent` 实例**（Layer 1），经 `ToolGateway` 包裹后只能调用授权工具。

### 4.3 Blackboard

`src/blackboard.py` 提供可选的共享写入面：

- 同 key 同 source → 覆盖
- 同 key 不同 source → 记录冲突，不静默覆盖
- 支持 TTL 与前缀读取

当前主路径由 **Orchestrator 直接写 `RepairState`**；Blackboard 用于冲突检测与扩展场景（多 Writer 实验）。

### 4.4 评测与消融

| 模块 | 职责 |
|------|------|
| `eval/runner.py` | 10 Case × orchestrator.repair → eval_report.json |
| `eval/baseline.py` | Single-Agent Orchestrator（全工具 ReAct） |
| `eval/ablation.py` | full / single / no_retriever × repetitions |
| `eval/metrics.py` | fix_rate、patch_precision、regression_rate |
| `eval/regression_check.py` | CI 回归门禁（对比基线报告） |

---

## 5. RepairState 状态变换

`RepairState`（`src/state.py`）是 Layer 2 的**唯一共享状态对象**，Orchestrator 持有并在阶段间传递。

### 5.1 字段与生产者

| 字段 | 类型 | 写入者 | 含义 |
|------|------|--------|------|
| `issue_input` | str | CLI | 原始 Issue |
| `repair_plan` | RepairPlan | Orchestrator._parse_issue | 语言、issue_type、嫌疑文件 |
| `suspect_locations` | list | Localizer | 精确行号与置信度 |
| `retrieved_context` | RetrievedContext | Retriever | 相关测试、调用链、片段 |
| `candidate_patches` | list | Patcher | unified diff + 说明 |
| `verification_result` | VerificationResult | Verifier | pytest / 容器结果 |
| `feedback` | str | Orchestrator | 验证失败摘要，喂给下一轮 Patcher |
| `retry_count` | int | Orchestrator | 当前重试轮次 |
| `status` | str | Orchestrator | 见下表 |
| `node_timings` | dict | 各阶段 | 毫秒级耗时 + token 用量 |

### 5.2 status 状态机

```
pending
   │ parse + localize/retrieve
   ▼
(localizing 中间态，代码中较少显式设置)
   │
   ▼
 patched  ←── skip_verify 或无 Verifier 时，有补丁即停
   │
   │ verify loop
   ├─ all_passed ──► fixed
   │
   ├─ fail + retry < max ──► 回滚 ──► feedback ──► 再 patch
   │
   └─ retry >= max ──► exhausted
```

### 5.3 自愈循环（Verifier 重试）

启用 pytest verify 时（默认）：

1. **Patcher 前**：`_snapshot_repo()` 保存工作区文件快照  
2. **Patcher 后**：应用补丁到工作区  
3. **Verifier**：本地 `pytest` 或 Docker 内测试  
4. **失败**：`_restore_repo_snapshot()` 还原 → `_build_feedback()` 写入 `state.feedback` → `retry_count += 1`  
5. **成功**：`status = "fixed"`，保留补丁

无补丁或 JSON 解析失败也会递增 `retry_count`，并给出针对性 `feedback`。

---

## 6. 安全模型

FixLoop 采用**纵深防御**：Layer 1 闸口 + Layer 2 权限网关 + 容器隔离。

```
┌─────────────────────────────────────────────────────────────┐
│ 第 1 层：路径锚定 (ToolContext)                             │
│   所有文件路径 resolve 到 workspace 内；拒绝 .. 逃逸        │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 第 2 层：审批策略 (approval_policy)                         │
│   auto / ask / never；高风险工具可拒绝 write/patch/shell    │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 第 3 层：配额 (QuotaEnforcer)                               │
│   限制 writes / shell / total 调用次数，防 runaway agent    │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 第 4 层：容器隔离 (SandboxManager)                          │
│   network_mode=none, mem_limit=4g, 一次验证一个容器         │
│   tar 传文件，无 bind mount；验证完即销毁                   │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 第 5 层：ToolGateway (Layer 2)                              │
│   Localizer 不能 write；Patcher 不能 ast_parse；声明式表    │
└─────────────────────────────────────────────────────────────┘
```

### 6.1 Layer 1：ToolExecutor 九道闸口

按序执行，任一失败返回结构化 `ToolExecutionResult`：

1. allowed_tools 白名单  
2. 工具存在检查  
3. 参数校验（含路径逃逸）  
4. 配额检查  
5. 重复调用检测（最近 2 次相同 → 拒绝）  
6. Dry-Run 短路  
7. 审批检查  
8. 执行前工作区快照  
9. 执行 + 执行后 diff 对比  

另：`security.py` 对 trace / artifact 做 API key 脱敏。

### 6.1.1 沙箱网络策略

Docker sandbox 默认 `network_mode=none`，容器内完全无网络访问。

| 场景 | 行为 | 原因 |
|------|------|------|
| `pip install` 第三方包 | 不工作 | 无外网 |
| `curl` / `wget` 外网资源 | 超时 | 无外网 |
| 访问宿主机 localhost | 不工作 | 独立网络命名空间 |

**Tradeoff**：`network=none` 意味着无法在容器内 `pip install` 运行时依赖（如 `requests`）。
所有依赖必须**预装在镜像**中（`sandbox/Dockerfile.python`）。

**降级路径**：Docker 不可用时自动回退到 `--skip-verify` 本地 pytest 验证，
此时网络可达但验证在宿主机执行（非隔离）。

### 6.2 Layer 2：ToolGateway 权限表

定义于 `src/middleware.py` 的 `REPAIR_PERMISSION_TABLE`：

- `write_file` / `patch_file` → 仅 **patcher**  
- `ast_parse` / `stack_parse` → 仅 **localizer**  
- `read_file` / `search` / `list_files` → 所有 Agent  

Agent 收到的是普通工具错误，**不知道被网关拦截**——避免 prompt 注入绕过。

### 6.3 补丁回滚

- **宿主机 verify**：Orchestrator 文件级快照（整 repo 文本）  
- **容器内 apply**：`PatchApplier` 逐 patch 应用，失败则 `_revert_all` 文件级回滚（`.bak.timestamp`）  

详见 ADR-010。

---

## 附录：目录对照

```
agent_runtime/     Layer 1 内核
src/
├── agents/        Localizer / Retriever / Patcher / Verifier 工厂
├── orchestrator.py
├── state.py       RepairState 类型
├── blackboard.py
├── middleware.py  ToolGateway
├── harness/       Docker + pytest + PatchApplier
├── tools/         AST / Stack / Git / Sandbox 工具
├── eval/          Runner / Ablation / Metrics
└── skills/        YAML 修复策略（Orchestrator 匹配）
```

更细的 Layer 1 模块导读见 [LAYER1_GUIDE.md](LAYER1_GUIDE.md)。
