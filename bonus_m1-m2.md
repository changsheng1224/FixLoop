# M1+M2 可改进与可额外实现功能探索

> 每个功能标注三项指标：
> - **P**（优先级）：P1 高回报 → P2 有价值 → P3 锦上添花
> - **C**（复杂度）：⭐ 几分钟 → ⭐⭐ 几小时 → ⭐⭐⭐ 1天 → ⭐⭐⭐⭐ 2-3天 → ⭐⭐⭐⭐⭐ 1周+
> - **I**（重要性）：⭐ 锦上添花 → ⭐⭐ 有更好 → ⭐⭐⭐ 值得做 → ⭐⭐⭐⭐ 显著提升 → ⭐⭐⭐⭐⭐ 核心竞争力

---

## 1. 控制循环 (agent_loop.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步工具超时**：每个工具调用增加独立 timeout 参数（当前只有 run_shell 有），防止模型调用 read_file 读取巨大文件时阻塞整个 AgentLoop
- **[P1] [C:⭐⭐⭐⭐ I:⭐⭐⭐⭐] 流式输出支持**：模型返回时逐 token 解析，一旦检测到 `<final>` 闭合标签或完整 `<tool>JSON</tool>` 块就开始下一步，不等完整响应。减少用户感知延迟 50%+
- **[P2] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐] 并行工具执行**：非冲突工具（如 list_files + search）在同一轮并行执行，减少等待时间。需分析工具依赖关系
- **[P2] [C:⭐ I:⭐⭐⭐] 自动重试退避**：retry 时增加指数退避（1s → 2s → 4s），避免在 API 错误时密集重试

---

## 2. 模型输出解析 (runtime.py)


- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 批量工具调用**：单次模型响应中包含多个 `<tool>` 块时全部解析（当前只取第一个匹配），减少往返轮数
- **[P2] [C:⭐ I:⭐⭐] 解析错误精确定位**：retry 时告知模型"在第 N 个字符处 `<tool` 标签未闭合"，而不是模糊提示
- **[P3] [C:⭐⭐ I:⭐⭐⭐] 思考链 (CoT) 提取**：从 DeepSeek thinking tokens 中提取推理过程，记录到 trace 供后续分析，但不占用 history token

---

## 3. 工具执行闸口 (tool_executor.py) 

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 闸口审计日志**：每次闸口拒绝记录到 `.agent/audit/` JSONL，含 timestamp / tool_name / gate / reason，用于安全审计和分析 Agent 行为模式
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 审批时展示 diff 预览**：write_file / patch_file 审批时显示 `[DRY RUN 预览]` 的内容预览，帮助用户做审批决策










## 4. 工具系统 (tools.py)

- **[P1] [C:⭐ I:⭐⭐⭐] read_file 上下文行**：`context_lines=3` 参数，在匹配行前后各多读 3 行，类似 grep -C
- **[P1] [C:⭐ I:⭐⭐] list_files 递归深度**：`depth=1` 参数限制递归层级，防止单层文件过多或无限递归
- **[P2] [C:⭐ I:⭐⭐] write_file 追加模式**：`append=True` 时追加而非覆盖，用于增量构建
- **[P2] [C:⭐⭐ I:⭐⭐] 文件编码自动检测**：read_file 对非 UTF-8 文件尝试 `chardet` 检测编码并自动转换

---

## 5. 上下文预算 (context_manager.py)

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐⭐] LLM 驱动的历史摘要**：当 history 超限时，用模型将前一半历史压缩为一句话摘要（`[Earlier: 你读取了 config.py 和 tools.py，确认了工具注册机制]`），替代当前的规则裁剪（注：M3 计划已包含，此处为 M2 提前实现）
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] 自适应预算**：模型返回 <final> 的第一轮不触发裁剪（说明是简单任务），连续 tool 多轮时逐步收紧 budget
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多模型 tokenizer 支持**：根据 config.provider 自动选择 tiktoken (OpenAI) / anthropic tokenizer / 通用 cl100k，而不是固定 cl100k_base
- **[P3] [C:⭐⭐ I:⭐⭐] Token 消耗仪表盘**：REPL 中 `/tokens` 命令实时显示各 section 的 token 分布柱状图（ASCII art）


## 6. Prompt Cache (prompt_prefix.py / clients.py)

- **[P1] [C:⭐ I:⭐⭐⭐] Cache 命中率统计**：记录 `cache_hits / cache_misses`，`/session` 命令显示当前会话的 cache 命中率
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 跨会话缓存共享**：同一 workspace 的多个 Agent 实例共享 prefix 缓存（需引入进程间通信）

---

## 7. 安全模块 (security.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐⭐] 命令注入检测**：`run_shell` 执行前对 command 做模式检测——管道符 `|`、重定向 `>`、反引号 `` ` `` 等高风险操作触发二次确认
- **[P3] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐⭐] 沙箱预览模式**：高风险工具在真正执行前，先在临时的 chroot/容器中预览效果

---



---

## 8. REPL (cli.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 命令历史**：集成 `readline`，支持 ↑↓ 键浏览历史命令、Ctrl-R 反向搜索
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 彩色输出**：`[DRY RUN]` 蓝色、`Error` 红色、`success` 绿色、工具调用黄色
- **[P2] [C:⭐ I:⭐⭐⭐] 多行输入**：`\` 续行符支持，方便粘贴多行 prompt

---

## 9. 模型客户端 (providers/clients.py)

- **[P1] [C:⭐ I:⭐⭐⭐] 响应时间统计**：每次 `complete()` 记录延迟，`/session` 显示 avg/p50/p99
- **[P2] [C:⭐ I:⭐⭐⭐] 请求重放**：记录最后一次请求的完整 payload 到 `.agent/last_request.json`，用于调试

---

## 10. 配置系统 (config.py)

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 配置 Profile**：`--profile dev|prod|test` 预设不同的 approval/max_steps/temperature
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 热重载**：REPL 中 `/config max_steps=10` 动态修改配置，不影响当前会话的 history

---


## 11. 整体增强

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一日志系统**：`agent_runtime.logger` 模块，INFO/DEBUG/ERROR 分级，输出到 stderr + `.agent/logs/`
