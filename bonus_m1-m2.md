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
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 条件分支控制**：支持模型在 `<tool>` 中声明 `on_error: retry|skip|fallback`，让 AgentLoop 根据工具结果自动选择路径
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 长时间运行任务队列**：超过 max_steps 的任务自动挂起到 `.agent/queue/`，支持后续恢复

---

## 2. 模型输出解析 (runtime.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐] OpenAI Function Call 格式兼容**：除了 `<tool>` XML 和 JSON，增加 `{"tool_calls":[{"function":{"name":"x","arguments":"{...}"}}]}` 格式的解析
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 批量工具调用**：单次模型响应中包含多个 `<tool>` 块时全部解析（当前只取第一个匹配），减少往返轮数
- **[P2] [C:⭐ I:⭐⭐] 解析错误精确定位**：retry 时告知模型"在第 N 个字符处 `<tool` 标签未闭合"，而不是模糊提示
- **[P2] [C:⭐⭐ I:⭐⭐] 转义处理**：文件内容中如果出现 `<tool>` 字面量（如 README 中的示例代码），支持 CDATA 或转义语法
- **[P3] [C:⭐⭐ I:⭐⭐⭐] 思考链 (CoT) 提取**：从 DeepSeek thinking tokens 中提取推理过程，记录到 trace 供后续分析，但不占用 history token

---

## 3. 工具执行闸口 (tool_executor.py) 

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 闸口审计日志**：每次闸口拒绝记录到 `.agent/audit/` JSONL，含 timestamp / tool_name / gate / reason，用于安全审计和分析 Agent 行为模式
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 审批时展示 diff 预览**：write_file / patch_file 审批时显示 `[DRY RUN 预览]` 的内容预览，帮助用户做审批决策
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 工具配额系统**：`max_writes_per_session=20` / `max_shell_per_session=10`，AgentLoop 的配额独立于闸口校验（注：M4 计划已包含，此处为 M2 提前实现）
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] 路径白名单/黑名单**：配置 `allowed_paths: ["src/", "tests/"]` / `denied_paths: [".env", "*.key"]`，在闸口 3 参数校验后、闸口 5 审批前检查
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 内容安全检查**：write_file 前对 content 做关键词扫描（如 `rm -rf /` / `eval(`），标记可疑内容并要求二次审批
- **[P3] [C:⭐⭐ I:⭐⭐] 执行结果分级**：不只看 exit_code，还分析 stderr 中的模式（warning / error / fatal），返回 `tool_severity: warning|error|fatal`

---

## 4. 工具系统 (tools.py)

- **[P1] [C:⭐ I:⭐⭐⭐] read_file 上下文行**：`context_lines=3` 参数，在匹配行前后各多读 3 行，类似 grep -C
- **[P1] [C:⭐ I:⭐⭐⭐] search 支持正则**：`regex=True` 时用 `re.search(pattern, line)` 替代子串匹配
- **[P1] [C:⭐ I:⭐⭐] list_files 递归深度**：`depth=1` 参数限制递归层级，防止单层文件过多或无限递归
- **[P2] [C:⭐ I:⭐⭐] write_file 追加模式**：`append=True` 时追加而非覆盖，用于增量构建
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] patch_file 多文件 diff**：支持 unified diff 格式输入，一次修补多个文件
- **[P2] [C:⭐⭐ I:⭐⭐] 文件编码自动检测**：read_file 对非 UTF-8 文件尝试 `chardet` 检测编码并自动转换
- **[P3] [C:⭐ I:⭐⭐⭐] 二进制文件检测**：read_file 前检查前 1024 字节中 null byte 比例，超过阈值拒绝读取并提示
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 工具降级链配置化**：将 rg→Python 降级抽象为通用降级链框架，所有工具可声明降级策略

---

## 5. 上下文预算 (context_manager.py)

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐⭐] LLM 驱动的历史摘要**：当 history 超限时，用模型将前一半历史压缩为一句话摘要（`[Earlier: 你读取了 config.py 和 tools.py，确认了工具注册机制]`），替代当前的规则裁剪（注：M3 计划已包含，此处为 M2 提前实现）
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] 自适应预算**：模型返回 <final> 的第一轮不触发裁剪（说明是简单任务），连续 tool 多轮时逐步收紧 budget
- **[P2] [C:⭐⭐⭐⭐ I:⭐⭐⭐] 智能 section 排序**：不是固定优先级，而是基于当前 task 的语义动态调整（如 "search for X" 时给 search 结果更高权重）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多模型 tokenizer 支持**：根据 config.provider 自动选择 tiktoken (OpenAI) / anthropic tokenizer / 通用 cl100k，而不是固定 cl100k_base
- **[P3] [C:⭐⭐ I:⭐⭐] Token 消耗仪表盘**：REPL 中 `/tokens` 命令实时显示各 section 的 token 分布柱状图（ASCII art）

---

## 6. Prompt Cache (prompt_prefix.py / clients.py)

- **[P1] [C:⭐ I:⭐⭐⭐] Cache 命中率统计**：记录 `cache_hits / cache_misses`，`/session` 命令显示当前会话的 cache 命中率
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] 增量 workspace 快照**：如果 workspace 只在 git_status / recent_commits 变了（其他不变），只重新计算变化部分的 hash，prefix 主体复用
- **[P2] [C:⭐⭐⭐⭐ I:⭐⭐] 多块缓存标记**：不只是 prefix 打 cache_control，Prefix 内部分 section（如工具列表）单独打 cache_control
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 跨会话缓存共享**：同一 workspace 的多个 Agent 实例共享 prefix 缓存（需引入进程间通信）

---

## 7. 安全模块 (security.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐⭐] 命令注入检测**：`run_shell` 执行前对 command 做模式检测——管道符 `|`、重定向 `>`、反引号 `` ` `` 等高风险操作触发二次确认
- **[P1] [C:⭐ I:⭐⭐⭐] 路径遍历防护增强**：除了 `commonpath`，增加 symlink 解析后的二次检测（`Path.resolve()` 已做，但可加显式日志）
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 文件内容安全扫描**：write_file / patch_file 执行前，对 content 做正则扫描（`os.system` / `subprocess` / `__import__`），防止 Agent 写入恶意代码
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 操作频率限制**：同一工具在 10 秒内被调用超过 5 次 → 触发 rate limit
- **[P3] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐⭐] 沙箱预览模式**：高风险工具在真正执行前，先在临时的 chroot/容器中预览效果

---

## 8. Dry-Run (tool_executor.py + cli.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐] Dry-Run 报告生成**：演习结束后输出结构化的执行计划报告（Markdown 格式），列出所有 [DRY RUN] 步骤及其预期影响
- **[P2] [C:⭐⭐⭐ I:⭐⭐] Dry-Run 与实际执行对比**：先 dry-run 再实际执行，对比计划与实际差异，标注偏差
- **[P2] [C:⭐⭐ I:⭐⭐] 部分 Dry-Run**：只对高风险工具 dry-run，只读工具正常执行，混合模式

---

## 9. REPL (cli.py)

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 命令历史**：集成 `readline`，支持 ↑↓ 键浏览历史命令、Ctrl-R 反向搜索
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] Tab 自动补全**：工具名补全、路径补全、`/` 命令补全
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 彩色输出**：`[DRY RUN]` 蓝色、`Error` 红色、`success` 绿色、工具调用黄色
- **[P2] [C:⭐ I:⭐⭐⭐] 多行输入**：`\` 续行符支持，方便粘贴多行 prompt
- **[P2] [C:⭐⭐ I:⭐⭐] 会话导入导出**：`/save` 和 `/load` 命令，保存/恢复 session JSON
- **[P3] [C:⭐⭐⭐ I:⭐⭐] Markdown 渲染**：模型输出中代码块语法高亮（依赖 `rich` 或 `pygments`）

---

## 10. 模型客户端 (providers/clients.py)

- **[P1] [C:⭐ I:⭐⭐⭐] 响应时间统计**：每次 `complete()` 记录延迟，`/session` 显示 avg/p50/p99
- **[P2] [C:⭐ I:⭐⭐⭐] 请求重放**：记录最后一次请求的完整 payload 到 `.agent/last_request.json`，用于调试
- **[P2] [C:⭐⭐⭐⭐ I:⭐] HTTP/2 支持**：`urllib` 不支持 HTTP/2，可考虑引入 `http.client.HTTPConnection` 或轻量 HTTP/2 库
- **[P3] [C:⭐⭐ I:⭐⭐⭐] 自适应超时**：根据历史响应时间动态调整 timeout，长 prompt 自动延长

---

## 11. 配置系统 (config.py)

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 配置 Profile**：`--profile dev|prod|test` 预设不同的 approval/max_steps/temperature
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 热重载**：REPL 中 `/config max_steps=10` 动态修改配置，不影响当前会话的 history
- **[P3] [C:⭐ I:⭐⭐] 配置校验增强**：base_url 格式校验（必须是合法 URL），api_key 最小长度校验

---

## 12. 工作区 (workspace.py)

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 文件变更检测**：`git diff --name-only HEAD` 显示自上次 commit 以来的变更文件列表
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 外部修改检测**：Agent 运行期间定时检查 workspace 文件是否被外部修改（对比上次快照）
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 多仓库支持**：WorkspaceContext 支持多个 repo_root（monorepo 场景）

---

## 13. 整体增强

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一日志系统**：`agent_runtime.logger` 模块，INFO/DEBUG/ERROR 分级，输出到 stderr + `.agent/logs/`
- **[P1] [C:⭐ I:⭐⭐⭐] 启动自检**：`python -m agent_runtime --check` 检查环境（Python 版本 / tiktoken / rg / git / .env 配置完整性）
- **[P2] [C:⭐⭐⭐⭐ I:⭐⭐⭐⭐] 插件化工具注册**：工具不再硬编码在 `build_tool_registry` 中，而是通过装饰器 `@register_tool(name, risky=True)` 自动发现
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] REST API 模式**：`python -m agent_runtime --serve :8000` 启动 HTTP 服务，POST `/ask` 接收 prompt
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐⭐] E2E 测试框架**：基于真实 API 的自动化回归测试套件（需要 API key，标记为 `@pytest.mark.e2e`）

---

## 评分维度说明

| 维度 | ⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
|------|------|------|------|------|------|
| **C (复杂度)** | 几分钟，改几行 | 几小时，改一个文件 | 1天，改多个文件 | 2-3天，新模块 | 1周+，架构级变更 |
| **I (重要性)** | 锦上添花 | 有更好 | 值得做 | 显著提升质量/体验 | 核心竞争力/面试亮点 |

---

## 优先级汇总

| 优先级 | 数量 | 代表条目 |
|:--:|:--:|------|
| **P1** | 17 项 | 流式输出 C:⭐⭐⭐⭐ I:⭐⭐⭐⭐、LLM摘要 C:⭐⭐⭐ I:⭐⭐⭐⭐⭐、命令注入检测 C:⭐⭐ I:⭐⭐⭐⭐⭐ |
| **P2** | 17 项 | 插件化工具注册 C:⭐⭐⭐⭐ I:⭐⭐⭐⭐、REST API C:⭐⭐⭐ I:⭐⭐⭐⭐、配额系统 C:⭐⭐⭐ I:⭐⭐⭐ |
| **P3** | 11 项 | 沙箱预览 C:⭐⭐⭐⭐⭐ I:⭐⭐⭐⭐、E2E 测试 C:⭐⭐⭐⭐ I:⭐⭐⭐、条件分支 C:⭐⭐⭐⭐ I:⭐⭐ |

**🏆 Top 5 最高投入产出比**（高重要性 + 低复杂度）：

| 排名 | 条目 | C | I | 模块 |
|:--:|------|:--:|:--:|------|
| 1 | LLM 驱动历史摘要 | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | context_manager |
| 2 | 命令注入检测 | ⭐⭐ | ⭐⭐⭐⭐⭐ | security |
| 3 | 统一日志系统 | ⭐⭐ | ⭐⭐⭐⭐ | 整体 |
| 4 | 批量工具调用 | ⭐⭐⭐ | ⭐⭐⭐⭐ | runtime |
| 5 | 流式输出支持 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | agent_loop |

**⚠ Top 3 最需谨慎规划**（高复杂度 + 不一定高回报）：

| 排名 | 条目 | C | I | 风险 |
|:--:|------|:--:|:--:|------|
| 1 | 并行工具执行 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | 依赖分析容易出错 |
| 2 | 沙箱预览模式 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | M6 已有 Docker 沙箱，此功能重复 |
| 3 | 跨会话缓存共享 | ⭐⭐⭐⭐ | ⭐⭐ | 复杂度高但 cache 利用率有限 |

**总计 45 项**，建议优先执行 P1 中 `I>=4` 且 `C<=3` 的 9 项（可在 M3-M4 中以副线推进）。
