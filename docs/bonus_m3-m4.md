# M3+M4 可改进与可额外实现功能探索

> 每个功能标注三项指标：
> - **P**（优先级）：P1 高回报 → P2 有价值 → P3 锦上添花
> - **C**（复杂度）：⭐ 几分钟 → ⭐⭐ 几小时 → ⭐⭐⭐ 1天 → ⭐⭐⭐⭐ 2-3天 → ⭐⭐⭐⭐⭐ 1周+
> - **I**（重要性）：⭐ 锦上添花 → ⭐⭐ 有更好 → ⭐⭐⭐ 值得做 → ⭐⭐⭐⭐ 显著提升 → ⭐⭐⭐⭐⭐ 核心竞争力/面试亮点

---

## 1. 工作记忆 — Working Memory (features/memory.py)

- **[P1] [C:⭐ I:⭐⭐⭐⭐] task_summary 自动生成**：当前 `set_task_summary` 直接取用户输入前 300 字，应改为调模型生成一句话摘要（"排查 calculator.py 的 TypeError"），而不是原始输入截断。模型不可用时退化为当前行为
- **[P2] [C:⭐⭐ I:⭐⭐⭐] recent_files 带访问时间戳**：每个文件记录不仅存路径，还存 `last_accessed_at`。`/memory` 命令按时间排序展示，方便用户理解 Agent 的工作顺序
- **[P2] [C:⭐ I:⭐⭐⭐] file_summaries 过期自动清理**：当前 freshness hash 变了才失效，应增加 TTL（如 30 分钟），超时自动清理。防止多次 ask 后摘要堆积

---

## 2. 事件记忆 — Episodic Memory (features/memory.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] retrieval_candidates 返回结果带分数**：当前只返回 note 列表，不返回匹配分数。加上分数后 Agent 可以对低分结果说"我不太确定，但可能和 X 相关"
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 笔记自动生成更丰富的 tags**：当前 tags 由调用方手动指定（如 `["shell", "error"]`），应从 note text 中自动提取关键词作为 tags（TF-IDF 或简单词频）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] episodic_notes 与 durable memory 自动关联**：当 episodic note 的 kind="decision" 时，自动 promote 到 durable memory（无需用户手动说"记住"）

---

## 3. 持久记忆 — Durable Memory (features/memory.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] retrieval 返回结果带 topic 信息**：当前 `retrieval(query)` 只返回文本，不标注属于哪个 topic。加上 topic 后 Agent 能说"根据你的偏好设置..."而不是模糊引用
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 自动清理过期条目**：每条 durable entry 带 `created_at` 时间戳，超过 N 天未被检索过的条目自动归档到 `topics/archive/`
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 冲突合并**：同一个 topic 下多条相似条目（cosine similarity > 0.8），提示用户合并或自动合并为一条
---

## 4. 语义记忆 — Semantic Memory (features/memory.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 模型预下载 + 离线可用**：当前首次加载需从 HuggingFace 下载 80MB 模型，GFW 下失败。应支持设置 `HF_ENDPOINT=https://hf-mirror.com` 或预下载到 `~/.cache/` 后离线加载
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 多语言 embedding 模型**：`all-MiniLM-L6-v2` 对中文支持一般。可换用 `paraphrase-multilingual-MiniLM-L12-v2`（118MB，支持 50+ 语言），中文同义词匹配更好
- **[P2] [C:⭐⭐ I:⭐⭐⭐] embedding 缓存**：相同文本不重复编码，缓存 embedding 结果到 `~/.agent/embedding_cache/`
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 增量索引**：当前每次 `add` 全量重算 cosine similarity。笔记超 100 条后应改用 FAISS 或 annoy 做近似最近邻搜索

---

## 5. 持久化系统 — TaskState / SessionStore / RunStore

- **[P1] [C:⭐ I:⭐⭐⭐⭐] task_state 记录耗时**：当前 TaskState 不记录各阶段耗时（prompt 组装 / 模型调用 / 工具执行）。加 `node_timings: dict` 字段，每次 `record_attempt` 和 `record_tool` 自动计算耗时
- **[P1] [C:⭐⭐ I:⭐⭐⭐] run 自动清理**：`.agent/runs/` 无限增长。应支持 `--max-runs 100` 配置，超出时自动删除最旧的 run 目录
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Session 导出/导入**：`/save <file>` 导出 session 为 JSON 文件，`/load <file>` 恢复。方便跨机器迁移会话
- **[P2] [C:⭐⭐ I:⭐⭐⭐] trace.jsonl 压缩**：超过 1000 行的 trace 自动 gzip 压缩（`.trace.jsonl.gz`），节省磁盘

---

## 6. 检查点与恢复 — Checkpoint + Resume

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] create_checkpoint 自动在 AgentLoop 中调用**：当前 `_finalize_run` 中已调用，但只在 ask 结束时。应在每轮工具执行后也创建 checkpoint（`trigger="tool_end"`），支持中断后从中间恢复
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 从 checkpoint 恢复时自动补做缺失步骤**：evaluate_resume_state 返回 `partial-stale` 时，除了告知用户，应自动重新执行 stale 文件相关的工具调用（如重新 read_file）

---

## 7. 安全模块 — Security (security.py)

- **[P1] [C:⭐ I:⭐⭐⭐⭐] redact_artifact 在 trace 写入前自动调用**：当前 redact_artifact 已实现但未自动集成到 RunStore。`append_trace` 写入前应自动调 `redact_artifact(payload)`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] run_shell 命令白名单**：除了 env 白名单，增加命令白名单模式——只允许 `pytest`、`pip`、`git` 等安全命令，拒绝 `rm -rf`、`curl | sh` 等

---

## 8. 对话摘要 — Summarization (context_manager.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 摘要质量指标**：记录每次摘要的 "压缩比"（原 token / 摘要 token）和关键实体保留率。摘要质量差时自动调高 trigger_tokens
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量摘要**：不是每次都从零生成摘要，而是在已有摘要基础上追加新内容（"Earlier: 读取了 config.py。Update: 又读取了 tools.py"）


---

## 9. API 熔断 — Circuit Breaker (providers/circuit_breaker.py)

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 熔断指标暴露**：`/session` 命令显示当前 CB 状态（closed/open/half_open）、失败计数、距恢复剩余秒数
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多 Provider 独立熔断**：当前一个 CB 服务所有 Provider。应每个 Provider 独立 CB（DeepSeek 挂了不误杀 Ollama）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 半开状态渐进恢复**：HALF_OPEN 不是只放 1 次，而是逐步增加（1→2→4→全开），像 TCP 拥塞控制

---

## 10. 确定性回放 — Deterministic Replay (replay.py)

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 完整参数回放**：当前 ReplayRunner 只从 trace 拿到 tool name，缺少 args。应修改 emit_trace 记录完整的 tool_args（经 redact 后），使 replay 可重新执行并对比结果
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 回归检测**：CI 中 `--replay all` 回放最近 N 个 trace，任一 diff 则阻断，作为回归门禁
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 可视化 diff**：replay 发现的差异以 unified diff 格式输出，像 `git diff` 一样可读



---

## 11. 进度回调 — ProgressCallback (callbacks.py)

- **[P1] [C:⭐ I:⭐⭐⭐] 彩色输出**：`[DRY RUN]` 蓝色、`Error` 红色、`success` 绿色、工具调用黄色。使用 ANSI escape codes（无需依赖）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 耗时统计**：每个工具执行后显示耗时 `✅ read_file (320 chars, 1.2s)`，帮助用户感知性能瓶颈

---

## 12. 工具配额 — QuotaEnforcer (tool_executor.py)

- **[P1] [C:⭐ I:⭐⭐⭐] 配额可通过 CLI 配置**：`--quota-writes 10 --quota-shell 5 --quota-total 30` 覆盖默认值

---


## 13. 整体增强 — Cross-cutting

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一配置 profile**：`--profile dev|prod|ci` 预设配额、CB 阈值、approval 策略的组合。dev 宽松（auto approve），prod 严格（ask），ci 最严格（never + 低配额）
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 健康检查端点**：`python -m agent_runtime --health` 检查所有模块初始化状态（模型连通性 / 工具可用性 / 存储可写性），返回 JSON