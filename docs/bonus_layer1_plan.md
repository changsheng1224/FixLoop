# Layer 1 可改进与可额外实现功能探索

> 覆盖 Agent 运行时全部模块。

---

## 1. 工具系统 — tools.py / tool_executor.py / schema_utils.py

- **[P1] [C:⭐ I:⭐⭐⭐] search 正则模式**：`regex=True` 用 `re.search` 替代子串匹配
- **[P1] [C:⭐ I:⭐⭐] list_files 递归深度**：`depth=1` 参数限制递归层级
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 审批时展示 diff 预览**：write_file / patch_file 审批时显示 `[DRY RUN 预览]`
- **[P1] [C:⭐ I:⭐⭐⭐] 配额 CLI 可配置**：`--quota-writes 10 --quota-shell 5 --quota-total 30`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] patch_file 支持统一 diff**：输入 unified diff 格式，一次修补多个文件多段
- **[P2] [C:⭐⭐ I:⭐⭐] 文件编码自动检测**：非 UTF-8 文件用 `chardet` 检测编码并转换

---

## 2. 安全模块 — security.py

- **[P1] [C:⭐ I:⭐⭐⭐⭐] redact_artifact 自动集成**：当前已实现但未自动接入 RunStore，`append_trace`/`write_report` 写入前应调用
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 文件内容安全扫描**：write_file 前检测 SQL 注入、硬编码密码、`exec`/`eval` 调用
- **[P3] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐⭐] 沙箱预览模式**：高风险工具在临时 chroot/容器中预览效果

---

## 3. 模型客户端 — providers/clients.py

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] Ollama streaming**：`stream=True` + SSE 解析，REPL 实时显示生成过程
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] OpenAI streaming**：SSE 流解析，`text/event-stream` content-type 处理
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多 Provider 独立熔断**：当前一个 CB 管所有，应为每个 Provider 独立实例

---

## 4. CLI / REPL — cli.py

- **[P1] [C:⭐ I:⭐⭐⭐] 命令历史**：集成 `readline`，↑↓ 浏览历史、Ctrl-R 搜索
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Tab 自动补全**：工具名 / 路径 / `/` 命令
- **[P2] [C:⭐ I:⭐⭐] 多行输入**：`\` 续行符支持
- **[P2] [C:⭐⭐ I:⭐⭐⭐] /save /load 会话**：导出/恢复 session JSON，跨机器迁移
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] REST API 模式**：`--serve :8000` 启动 HTTP，POST `/ask` + GET `/session/{id}`
- **[P3] [C:⭐⭐ I:⭐⭐] Markdown 渲染**：REPL 中代码块语法高亮

---

## 5. 进度回调 — callbacks.py


---

## 6. 熔断与回放 — circuit_breaker.py / replay.py

- **[P1] [C:⭐ I:⭐⭐⭐] 熔断指标暴露**：`/session` 显示 CB 状态、失败计数、距恢复剩余秒数
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] replay 完整参数回放**：emit_trace 记录完整 tool_args（经 redact），replay 可重新执行对比
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 回归检测**：CI 中 `--replay all` 回放最近 N 个 trace，任一 diff 阻断
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 可视化 diff**：replay 差异以 unified diff 格式输出
- **[P3] [C:⭐⭐ I:⭐⭐] 熔断事件 trace**：状态切换时发射 `circuit_opened`/`circuit_closed` 事件

---

## 7. 持久化与恢复 — task_state / stores / checkpoint


- **[P1] [C:⭐⭐ I:⭐⭐⭐] run 自动清理**：`.agent/runs/` 无限增长，`--max-runs 100` 自动删旧
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] checkpoint 每轮创建**：当前只在 ask 结束时创建，应在每轮工具执行后也创建
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 自动补做缺失步骤**：partial-stale 时自动重新读文件 + 执行
- **[P2] [C:⭐⭐ I:⭐⭐⭐] trace.jsonl 压缩**：超 1000 行自动 gzip

---

## 8. 上下文工程 — TokenBudget / ContextManager / 压缩 / 摘要

- **[P1] [C:⭐ I:⭐⭐⭐] 多模型 tokenizer 切换**：根据 config.provider 自动选择对应 tokenizer
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] memory section 优先级排序**：超出预算时按 task > modified > read > old summaries 取舍
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量压缩**：在上一轮压缩结果上追加新条目
- **[P2] [C:⭐ I:⭐⭐⭐] build() metadata 暴露**：在 `/session` 或 trace 中展示
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 动态预算分配**：简单任务收紧预算，复杂任务放宽
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 摘要质量评估**：记录压缩比和关键实体保留率，差时自动调整
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 摘要触发优化**：当前只看 token，应同时考虑轮数（>10 轮即触发）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量摘要**：已有摘要基础上追加，而非每次从头生成

---

## 9. Prompt 工程 — prompt_prefix.py / context_manager.py

- **[P1] [C:⭐ I:⭐⭐⭐⭐] Cache 命中率统计**：`/session` 显示 `cache_hits / cache_misses`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] few-shot 示例从文件加载**：`.agent/examples.md` → TOOL_EXAMPLES

---

## 10. 模型输出解析 — runtime.py

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 批量工具调用**：单次响应中多个 `<tool>` 块全部解析，减少往返轮数
- **[P2] [C:⭐ I:⭐⭐] 解析错误精确定位**：retry 时告知第 N 个字符处的具体问题
- **[P3] [C:⭐⭐ I:⭐⭐⭐] 思考链 (CoT) 提取**：从 thinking tokens 提取推理过程

---

## 11. 控制循环 — agent_loop.py

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步工具超时**：每个工具调用增加独立 timeout，防止单个工具阻塞
- **[P1] [C:⭐⭐⭐⭐ I:⭐⭐⭐⭐] 流式输出支持**：逐 token 解析，不等完整响应，减少感知延迟 50%+
- **[P2] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐] 并行工具执行**：非冲突工具在同一轮并行执行
- **[P2] [C:⭐ I:⭐⭐⭐] 自动重试退避**：retry 时指数退避（1s→2s→4s）

---

## 12. 配置系统 — config.py

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 配置 Profile**：`--profile dev|prod|test` 预设 approval/max_steps/temperature
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 热重载**：REPL 中 `/config max_steps=10` 动态修改

---

## 13. 记忆系统 — features/memory/

### 13.1 工作记忆

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] task_summary 模型生成**：调 light_client 生成一句话摘要，模型不可用时退化为截断
- **[P2] [C:⭐ I:⭐⭐⭐] recent_files 带访问时间戳**：`/memory` 按时间排序展示
- **[P2] [C:⭐ I:⭐⭐⭐] file_summaries TTL 自动过期**：30 分钟过期清理，防止堆积

### 13.2 事件记忆

- **[P2] [C:⭐⭐ I:⭐⭐⭐] episodic → durable 自动晋升**：kind="decision" 且多次被检索的笔记自动 promote

### 13.3 持久记忆

- **[P1] [C:⭐⭐ I:⭐⭐⭐] 条目带时间戳 + 自动归档**：超 N 天未检索自动移到 archive
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 冲突检测与合并**：同 topic 相似条目提示合并

### 13.4 语义记忆

- **[P2] [C:⭐ I:⭐⭐⭐] embedding 缓存**：相同文本不重复编码

### 13.5 整体

- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 记忆优先级仲裁**：超出 800 token 时按 task > files > summaries 取舍
- **[P3] [C:⭐⭐⭐ I:⭐⭐⭐] 上下文感知记忆**：根据任务类型自动调整检索策略

---

## 14. 整体增强

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一配置 profile**：`--profile dev|prod|ci` 预设 quota/CB/approval 组合
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 健康检查**：`--health` 检查模型连通/工具可用/存储可写，返回 JSON
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一日志系统**：`agent_runtime.logger` 模块，INFO/DEBUG/ERROR 分级
- **[P1] [C:⭐ I:⭐⭐⭐] 启动自检**：`--check` 检查 Python 版本/tiktoken/rg/git/.env 完整性
- **[P2] [C:⭐⭐⭐⭐ I:⭐⭐⭐] WebSocket 模式**：实时推送 Agent 执行过程给前端
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐⭐] 分布式追踪**：多 Agent 协作时的全链路 trace
