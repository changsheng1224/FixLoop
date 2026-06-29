# M1+M2 可改进与可额外实现功能探索

> 以下功能按模块排列，按投入产出比分三档：**P1 高回报**（投入小、效果明显）、**P2 有价值**（需要一定投入）、**P3 锦上添花**（可选的深度增强）。

---

## 1. 控制循环 (agent_loop.py)

- **[P1] 单步工具超时**：每个工具调用增加独立 timeout 参数（当前只有 run_shell 有），防止模型调用 read_file 读取巨大文件时阻塞整个 AgentLoop
- **[P1] 流式输出支持**：模型返回时逐 token 解析，一旦检测到 `<final>` 闭合标签或完整 `<tool>JSON</tool>` 块就开始下一步，不等完整响应。减少用户感知延迟 50%+
- **[P2] 并行工具执行**：非冲突工具（如 list_files + search）在同一轮并行执行，减少等待时间。需分析工具依赖关系
- **[P2] 自动重试退避**：retry 时增加指数退避（1s → 2s → 4s），避免在 API 错误时密集重试
- **[P3] 条件分支控制**：支持模型在 `<tool>` 中声明 `on_error: retry|skip|fallback`，让 AgentLoop 根据工具结果自动选择路径
- **[P3] 长时间运行任务队列**：超过 max_steps 的任务自动挂起到 `.agent/queue/`，支持后续恢复

---

## 2. 模型输出解析 (runtime.py)

- **[P1] OpenAI Function Call 格式兼容**：除了 `<tool>` XML 和 JSON，增加 `{"tool_calls":[{"function":{"name":"x","arguments":"{...}"}}]}` 格式的解析
- **[P1] 批量工具调用**：单次模型响应中包含多个 `<tool>` 块时全部解析（当前只取第一个匹配），减少往返轮数
- **[P2] 解析错误精确定位**：retry 时告知模型"在第 N 个字符处 `<tool` 标签未闭合"，而不是模糊提示
- **[P2] 转义处理**：文件内容中如果出现 `<tool>` 字面量（如 README 中的示例代码），支持 CDATA 或转义语法
- **[P3] 思考链 (CoT) 提取**：从 DeepSeek thinking tokens 中提取推理过程，记录到 trace 供后续分析，但不占用 history token

---

## 3. 工具执行闸口 (tool_executor.py)

- **[P1] 闸口审计日志**：每次闸口拒绝记录到 `.agent/audit/` JSONL，含 timestamp / tool_name / gate / reason，用于安全审计和分析 Agent 行为模式
- **[P1] 审批时展示 diff 预览**：write_file / patch_file 审批时显示 `[DRY RUN 预览]` 的内容预览，帮助用户做审批决策
- **[P2] 工具配额系统**：`max_writes_per_session=20` / `max_shell_per_session=10`，AgentLoop 的配额独立于闸口校验
- **[P2] 路径白名单/黑名单**：配置 `allowed_paths: ["src/", "tests/"]` / `denied_paths: [".env", "*.key"]`，在闸口 3 参数校验后、闸口 5 审批前检查
- **[P3] 内容安全检查**：write_file 前对 content 做关键词扫描（如 `rm -rf /` / `eval(`），标记可疑内容并要求二次审批
- **[P3] 执行结果分级**：不只看 exit_code，还分析 stderr 中的模式（warning / error / fatal），返回 `tool_severity: warning|error|fatal`

---

## 4. 工具系统 (tools.py)

- **[P1] read_file 上下文行**：`context_lines=3` 参数，在匹配行前后各多读 3 行，类似 grep -C
- **[P1] search 支持正则**：`regex=True` 时用 `re.search(pattern, line)` 替代子串匹配
- **[P1] list_files 递归深度**：`depth=1` 参数限制递归层级，防止单层文件过多或无限递归
- **[P2] write_file 追加模式**：`append=True` 时追加而非覆盖，用于增量构建
- **[P2] patch_file 多文件 diff**：支持 unified diff 格式输入，一次修补多个文件
- **[P2] 文件编码自动检测**：read_file 对非 UTF-8 文件尝试 `chardet` 检测编码并自动转换
- **[P3] 二进制文件检测**：read_file 前检查前 1024 字节中 null byte 比例，超过阈值拒绝读取并提示
- **[P3] 工具降级链配置化**：将 rg→Python 降级抽象为通用降级链框架，所有工具可声明降级策略

---

## 5. 上下文预算 (context_manager.py)

- **[P1] LLM 驱动的历史摘要**：当 history 超限时，用模型将前一半历史压缩为一句话摘要（`[Earlier: 你读取了 config.py 和 tools.py，确认了工具注册机制]`），替代当前的规则裁剪
- **[P1] 自适应预算**：模型返回 <final> 的第一轮不触发裁剪（说明是简单任务），连续 tool 多轮时逐步收紧 budget
- **[P2] 智能 section 排序**：不是固定优先级，而是基于当前 task 的语义动态调整（如 "search for X" 时给 search 结果更高权重）
- **[P2] 多模型 tokenizer 支持**：根据 config.provider 自动选择 tiktoken (OpenAI) / anthropic tokenizer / 通用 cl100k，而不是固定 cl100k_base
- **[P3] Token 消耗仪表盘**：REPL 中 `/tokens` 命令实时显示各 section 的 token 分布柱状图（ASCII art）

---

## 6. Prompt Cache (prompt_prefix.py / clients.py)

- **[P1] Cache 命中率统计**：记录 `cache_hits / cache_misses`，`/session` 命令显示当前会话的 cache 命中率
- **[P1] 增量 workspace 快照**：如果 workspace 只在 git_status / recent_commits 变了（其他不变），只重新计算变化部分的 hash，prefix 主体复用
- **[P2] 多块缓存标记**：不只是 prefix 打 cache_control，Prefix 内部分 section（如工具列表）单独打 cache_control
- **[P3] 跨会话缓存共享**：同一 workspace 的多个 Agent 实例共享 prefix 缓存（需引入进程间通信）

---

## 7. 安全模块 (security.py)

- **[P1] 命令注入检测**：`run_shell` 执行前对 command 做模式检测——管道符 `|`、重定向 `>`、反引号 `` ` `` 等高风险操作触发二次确认
- **[P1] 路径遍历防护增强**：除了 `commonpath`，增加 symlink 解析后的二次检测（`Path.resolve()` 已做，但可加显式日志）
- **[P2] 文件内容安全扫描**：write_file / patch_file 执行前，对 content 做正则扫描（`os.system` / `subprocess` / `__import__`），防止 Agent 写入恶意代码
- **[P2] 操作频率限制**：同一工具在 10 秒内被调用超过 5 次 → 触发 rate limit
- **[P3] 沙箱预览模式**：高风险工具在真正执行前，先在临时的 chroot/容器中预览效果

---

## 8. Dry-Run (tool_executor.py + cli.py)

- **[P1] Dry-Run 报告生成**：演习结束后输出结构化的执行计划报告（Markdown 格式），列出所有 [DRY RUN] 步骤及其预期影响
- **[P2] Dry-Run 与实际执行对比**：先 dry-run 再实际执行，对比计划与实际差异，标注偏差
- **[P2] 部分 Dry-Run**：只对高风险工具 dry-run，只读工具正常执行，混合模式

---

## 9. REPL (cli.py)

- **[P1] 命令历史**：集成 `readline`，支持 ↑↓ 键浏览历史命令、Ctrl-R 反向搜索
- **[P1] Tab 自动补全**：工具名补全、路径补全、`/` 命令补全
- **[P2] 彩色输出**：`[DRY RUN]` 蓝色、`Error` 红色、`success` 绿色、工具调用黄色
- **[P2] 多行输入**：`\` 续行符支持，方便粘贴多行 prompt
- **[P2] 会话导入导出**：`/save` 和 `/load` 命令，保存/恢复 session JSON
- **[P3] Markdown 渲染**：模型输出中代码块语法高亮（依赖 `rich` 或 `pygments`）

---

## 10. 模型客户端 (providers/clients.py)

- **[P1] 响应时间统计**：每次 `complete()` 记录延迟，`/session` 显示 avg/p50/p99
- **[P2] 请求重放**：记录最后一次请求的完整 payload 到 `.agent/last_request.json`，用于调试
- **[P2] HTTP/2 支持**：`urllib` 不支持 HTTP/2，可考虑引入 `http.client.HTTPConnection` 或轻量 HTTP/2 库
- **[P3] 自适应超时**：根据历史响应时间动态调整 timeout，长 prompt 自动延长

---

## 11. 配置系统 (config.py)

- **[P2] 配置 Profile**：`--profile dev|prod|test` 预设不同的 approval/max_steps/temperature
- **[P2] 热重载**：REPL 中 `/config max_steps=10` 动态修改配置，不影响当前会话的 history
- **[P3] 配置校验增强**：base_url 格式校验（必须是合法 URL），api_key 最小长度校验

---

## 12. 工作区 (workspace.py)

- **[P2] 文件变更检测**：`git diff --name-only HEAD` 显示自上次 commit 以来的变更文件列表
- **[P2] 外部修改检测**：Agent 运行期间定时检查 workspace 文件是否被外部修改（对比上次快照）
- **[P3] 多仓库支持**：WorkspaceContext 支持多个 repo_root（monorepo 场景）

---

## 13. 整体增强

- **[P1] 统一日志系统**：`agent_runtime.logger` 模块，INFO/DEBUG/ERROR 分级，输出到 stderr + `.agent/logs/`
- **[P1] 启动自检**：`python -m agent_runtime --check` 检查环境（Python 版本 / tiktoken / rg / git / .env 配置完整性）
- **[P2] 插件化工具注册**：工具不再硬编码在 `build_tool_registry` 中，而是通过装饰器 `@register_tool(name, risky=True)` 自动发现
- **[P2] REST API 模式**：`python -m agent_runtime --serve :8000` 启动 HTTP 服务，POST `/ask` 接收 prompt
- **[P3] E2E 测试框架**：基于真实 API 的自动化回归测试套件（需要 API key，标记为 `@pytest.mark.e2e`）

---

## 优先级汇总

| 优先级 | 数量 | 建议执行时机 |
|:--:|:--:|------|
| **P1** | 17 项 | M3-M4 开发中顺手实现，不额外占用 sprint |
| **P2** | 17 项 | M8 打磨阶段选择性实现 |
| **P3** | 11 项 | 面试准备时挑 2-3 个作为"进一步工作"的回答素材 |

**总计 45 项**，优先执行 P1 可在不打断主开发节奏的前提下大幅提升工程质量。
