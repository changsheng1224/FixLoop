# Layer 1 可改进与可额外实现功能探索

> 覆盖当前项目全部模块（排除已在 bonus_memory / bonus_context 中详述的条目）。

---

## 1. 工具系统 — tools.py / schema_utils.py / tool_context.py

- **[P1] [C:⭐ I:⭐⭐⭐] read_file 上下文行**：`context_lines=3` 参数，匹配行前后各多读 3 行，类似 grep -C
- **[P1] [C:⭐ I:⭐⭐⭐] search 正则模式**：`regex=True` 用 `re.search` 替代子串匹配
- **[P1] [C:⭐ I:⭐⭐] write_file append 模式**：`append=True` 追加而非覆盖，用于增量构建
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 工具注册改为装饰器**：`@register_tool(name, risky=True)` 替代硬编码 `build_tool_registry`，第三方可扩展
- **[P2] [C:⭐⭐ I:⭐⭐⭐] patch_file 支持统一 diff**：输入 unified diff 格式，一次修补多个文件多段
- **[P2] [C:⭐⭐ I:⭐⭐] 文件编码自动检测**：非 UTF-8 文件用 `chardet` 检测编码并转换
- **[P2] [C:⭐ I:⭐⭐⭐] search 结果上下文**：匹配行前后各显示 1 行（像 rg -C 1）
- **[P2] [C:⭐ I:⭐⭐] list_files 支持 glob 过滤**：`pattern="*.py"` 只显示匹配文件
- **[P3] [C:⭐ I:⭐⭐⭐] 二进制文件检测**：read_file 前检查 null byte 比例，超阈值拒绝并提示
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 工具降级链配置化**：抽象为通用框架，所有工具声明降级策略

---

## 2. 安全模块 — security.py

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 命令注入检测**：run_shell 执行前检测管道 `|`、重定向 `>`、反引号、`eval` 等高风险模式
- **[P1] [C:⭐ I:⭐⭐⭐] redact_artifact 集成到所有输出**：当前已接入 trace/report，应覆盖 prompt 日志、错误输出
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 命令白名单模式**：只允许 `pytest`/`pip`/`git`/`ls` 等安全命令
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 文件内容安全扫描**：write_file 前检测 SQL 注入、硬编码密码、`exec`/`eval` 调用
- **[P2] [C:⭐ I:⭐⭐⭐] 操作审计日志**：高风险操作记录到 `.agent/audit/` JSONL，含时间/工具/参数/审批
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 沙箱预览**：高风险工具在临时 chroot/容器中预览效果再决定是否真执行

---

## 3. 模型客户端 — providers/clients.py

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] Ollama streaming**：`stream=True` + SSE 解析，REPL 实时显示生成过程
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] OpenAI streaming**：SSE 流解析，`text/event-stream` content-type 处理
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 响应时间统计暴露**：每次 complete() 记录延迟，`/session` 显示 avg/p50/p99
- **[P2] [C:⭐ I:⭐⭐⭐] 请求重放调试**：记录最后一次请求 payload 到 `.agent/last_request.json`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多 Provider 独立熔断**：当前一个 CB 管所有，应为每个 Provider 独立实例
- **[P3] [C:⭐ I:⭐⭐⭐] 自适应超时**：根据历史响应时间动态调整 timeout
- **[P3] [C:⭐⭐ I:⭐⭐] 模型列表自动发现**：Ollama `/api/tags` 获取本地模型，`--model` tab 补全

---

## 4. CLI / REPL — cli.py

- **[P1] [C:⭐ I:⭐⭐⭐] 命令历史**：集成 `readline`，↑↓ 浏览历史、Ctrl-R 搜索
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 彩色输出**：`[DRY RUN]` 蓝、Error 红、success 绿、工具调用黄（ANSI）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Tab 自动补全**：工具名 / 路径 / `/` 命令
- **[P2] [C:⭐ I:⭐⭐] 多行输入**：`\` 续行符支持
- **[P2] [C:⭐⭐ I:⭐⭐⭐] /save /load 会话**：`/save <file>` 导出，`/load <file>` 恢复，跨机器迁移
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] REST API 模式**：`--serve :8000` 启动 HTTP，POST `/ask` + GET `/session/{id}`
- **[P3] [C:⭐⭐ I:⭐⭐] Markdown 渲染**：REPL 中代码块语法高亮

---

## 5. 工作区 — workspace.py

- **[P1] [C:⭐ I:⭐⭐⭐] git_status 截断**：防止大仓库 untracked 文件撑爆 prompt（限制 15 行）
- **[P1] [C:⭐ I:⭐⭐⭐] git log 加作者和时间**：`--format="%h %s (%ar)"` 替代当前 `--oneline`
- **[P2] [C:⭐ I:⭐⭐⭐] git_diff 信息**：`git diff --stat HEAD` 追加变更概览，Agent 感知当前改动
- **[P2] [C:⭐⭐ I:⭐⭐⭐] fingerprint 优化**：排除 untracked 文件，减少不必要的 cache 失效
- **[P3] [C:⭐⭐ I:⭐⭐] 路径统一正斜杠**：Windows 反斜杠在 prompt 中像转义符
- **[P3] [C:⭐⭐ I:⭐⭐] DOC_NAMES 可配置**：通过 `.env` 或 CLI 指定额外文档

---

## 6. 检查点与恢复 — checkpoint.py

- **[P1] [C:⭐⭐ I:⭐⭐⭐] 每轮工具后创建 checkpoint**：当前只在 ask 结束时创建，中断后无法从中间恢复
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 自动补做缺失步骤**：partial-stale 时自动重新读文件 + 执行
- **[P2] [C:⭐⭐ I:⭐⭐⭐] /checkpoints 命令**：列出所有 checkpoint 时间线和状态变化
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 跨 session 恢复**：从另一个 session 的 checkpoint 恢复

---

## 7. 熔断与回放 — circuit_breaker.py / replay.py

- **[P1] [C:⭐ I:⭐⭐⭐] 熔断指标暴露**：`/session` 显示 CB 状态、失败计数、距恢复剩余秒数
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 半开渐进恢复**：HALF_OPEN 逐步增加（1→2→4→全开），类似 TCP 拥塞控制
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] replay 完整参数回放**：emit_trace 记录完整 tool_args（经 redact），replay 可重新执行对比
- **[P2] [C:⭐ I:⭐⭐⭐] replay 回归门禁**：CI 中 `--replay all`，任一 diff 阻断
- **[P3] [C:⭐⭐ I:⭐⭐] 熔断事件 trace**：状态切换时发射 `circuit_opened`/`circuit_closed` 事件

---

## 8. 持久化 — task_state.py / session_store.py / run_store.py

- **[P1] [C:⭐ I:⭐⭐⭐⭐] task_state 记录各阶段耗时**：`node_timings: {prompt_build_ms, model_call_ms, tool_exec_ms}`，report 含耗时分布
- **[P1] [C:⭐ I:⭐⭐⭐] run 自动清理**：`.agent/runs/` 无限增长，`--max-runs 100` 自动删旧
- **[P2] [C:⭐⭐ I:⭐⭐⭐] trace.jsonl 压缩**：超 1000 行自动 gzip，节省磁盘
- **[P2] [C:⭐⭐ I:⭐⭐] report.json 分布图**：`/report` 用 ASCII art 展示 token 和耗时分布
- **[P3] [C:⭐⭐⭐ I:⭐⭐⭐] OpenTelemetry 导出**：trace→OLTP，接入 Jaeger/Zipkin

---

## 9. System Prompt — prompt_prefix.py

- **[P1] [C:⭐ I:⭐⭐⭐⭐] rules 根据模式动态注入**：dry-run 时追加 "不要实际修改文件"，approval=auto 时移除 "需要审批" 相关规则
- **[P2] [C:⭐⭐ I:⭐⭐⭐] few-shot 示例从文件加载**：`.agent/examples.md` → TOOL_EXAMPLES
- **[P3] [C:⭐ I:⭐⭐] 角色定义可配置**：`_system_persona` 从 CLI/env 覆盖

---

## 10. 整体增强

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一配置 profile**：`--profile dev|prod|ci` 预设 quota/CB/approval 组合
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 健康检查**：`python -m agent_runtime --health` 检查模型连通/工具可用/存储可写
- **[P1] [C:⭐ I:⭐⭐⭐] 启动自检**：`--check` 检查 Python 版本/tiktoken/rg/git/.env 完整性
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] Plugin 系统**：`@register_tool` + `@register_provider` 装饰器注册
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] 跨平台测试**：当前只测 Windows，应加 Linux CI
- **[P2] [C:⭐⭐⭐⭐ I:⭐⭐⭐] WebSocket 模式**：实时推送 Agent 执行过程给前端
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐⭐] 分布式追踪**：多 Agent 协作时的全链路 trace
- **[P3] [C:⭐⭐ I:⭐⭐] 国际化**：CLI 和 prompt 支持 `--lang zh/en`

---

## 评分维度说明

| 维度 | ⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
|------|------|------|------|------|------|
| **C (复杂度)** | 几分钟 | 几小时 | 1天 | 2-3天 | 1周+ |
| **I (重要性)** | 锦上添花 | 有更好 | 值得做 | 显著提升 | 核心竞争力/面试亮点 |

---

## 优先级汇总

| 优先级 | 数量 | 代表条目 |
|:--:|:--:|------|
| **P1** | 10 项 | task_state 耗时 `C:⭐ I:⭐⭐⭐⭐`、命令注入检测 `C:⭐⭐ I:⭐⭐⭐⭐`、健康检查 `C:⭐⭐ I:⭐⭐⭐⭐` |
| **P2** | 16 项 | REST API `C:⭐⭐ I:⭐⭐⭐⭐`、replay 完整回放 `C:⭐⭐⭐ I:⭐⭐⭐⭐`、Plugin 系统 `C:⭐⭐⭐ I:⭐⭐⭐⭐` |
| **P3** | 10 项 | WebSocket `C:⭐⭐⭐⭐ I:⭐⭐⭐`、OpenTelemetry `C:⭐⭐⭐ I:⭐⭐⭐` |

**🏆 Top 5 最高投入产出比**：

| 排名 | 条目 | C | I | 模块 |
|:--:|------|:--:|:--:|------|
| 1 | task_state 记录耗时分布 | ⭐ | ⭐⭐⭐⭐ | 持久化 |
| 2 | rules 动态注入 | ⭐ | ⭐⭐⭐⭐ | Prompt |
| 3 | 命令注入检测 | ⭐⭐ | ⭐⭐⭐⭐ | 安全 |
| 4 | 统一配置 profile | ⭐⭐ | ⭐⭐⭐⭐ | 整体 |
| 5 | 健康检查 | ⭐⭐ | ⭐⭐⭐⭐ | 整体 |

**总计 36 项**。

### 所有 Bonus 文档汇总

| 文档 | 覆盖范围 | 条数 |
|------|------|:--:|
| `bonus_m1-m2.md` | M1+M2 | 45 |
| `bonus_m3-m4.md` | M3+M4 | 44 |
| `bonus_memory.md` | 记忆系统专项 | 29 |
| `bonus_context.md` | 上下文工程专项 | 30 |
| `bonus_layer1.md` | 其余模块 + 整体 | 36 |
| **合计** | | **184** |
