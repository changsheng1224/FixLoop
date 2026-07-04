# Layer 1 可改进与可额外实现功能探索

> 覆盖 `agent_runtime/` 运行时内核全部模块。与 `bonus_layer2_plan.md`、`bonus_m5-m6.md`、`bonus.md` **互补、不重复**；请自行筛选。  
> 基线：`master` @ PR #83 · `484 tests`。标注 **✅** 表示代码中已有基础实现，条目描述的是增强或补全。

---

## 1. 工具系统 — tools.py / tool_executor.py / schema_utils.py

- **[P1] [C:⭐ I:⭐⭐⭐] search 正则模式**：`regex=True` 用 `re.search` 替代子串匹配
- **[P1] [C:⭐ I:⭐⭐] list_files 递归深度**：`depth=1` 参数限制递归层级
- **[P1] [C:⭐ I:⭐⭐⭐] 审批时展示 diff 预览**：write_file / patch_file 审批时显示 `[DRY RUN 预览]` 或 patch 前后片段
- **[P2] [C:⭐⭐ I:⭐⭐⭐] patch_file 支持统一 diff**：输入 unified diff 格式，一次修补多个文件多段
- **[P2] [C:⭐⭐ I:⭐⭐] 文件编码自动检测**：非 UTF-8 文件用 `chardet` 检测编码并转换
- **[P1] [C:⭐ I:⭐⭐⭐] write_file 原子写**：先写 `.tmp` 再 `replace`，避免半写文件
- **[P2] [C:⭐ I:⭐⭐⭐] search 结果上限**：`max_results` 参数 + 截断提示，防止 rg 输出撑爆 context
- **[P2] [C:⭐ I:⭐⭐] list_files glob 过滤**：`pattern="*.py"` 只列匹配文件
- **[P2] [C:⭐ I:⭐⭐⭐] patch_file 可选 fuzzy**：`old_text` 未精确匹配时 Levenshtein 提示最接近片段
- **[P2] [C:⭐ I:⭐⭐] IGNORED_PATH_NAMES 可配置**：从 `AgentConfig` 或 env 追加忽略目录
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 快照性能优化**：Gate 8 全树 SHA256 对大 repo 极慢，改为仅追踪 `recent_files` + 目标 path
- **[P1] [C:⭐ I:⭐⭐⭐] permission_denied 写 trace**：`tool_policy` 拒绝时 `_emit` 事件，便于 L2 演示与审计
- **[P2] [C:⭐ I:⭐⭐⭐] 非交互审批回调**：`approval_policy=ask` 时注入 `approve_fn`，供 headless/CI 自定义逻辑
- **[P2] [C:⭐⭐ I:⭐⭐] schema 字符串长度约束**：`auto_validate` 对 `content`/`command` 设 max_len，防模型灌入巨型参数

---

## 2. 安全模块 — security.py

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ session 持久化脱敏**：`SessionStore.save` 前对 history 中 tool_args 调用 `redact_artifact`（trace/report 已接入）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 文件内容安全扫描**：write_file 前检测硬编码密码、`eval`/`exec` 等危险模式（规则可关）
- **[P2] [C:⭐ I:⭐⭐⭐] Shell 命令 denylist**：`run_shell` 拦截 `rm -rf /`、`curl | sh` 等模式（可配置）
- **[P2] [C:⭐ I:⭐⭐⭐] 符号链接逃逸检测**：`ToolContext.resolve` 对 symlink 做 `resolve()` 二次校验，防 `../../etc/passwd`
- **[P3] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐⭐] 沙箱预览模式**：高风险工具在临时目录或容器中预览效果后再落盘

---

## 3. 模型客户端 — providers/clients.py / bootstrap.py

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] Ollama streaming**：`stream=True` + SSE/chunk 解析，REPL 实时显示生成过程
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] OpenAI / Anthropic 原生 streaming**：`text/event-stream` 增量解析
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多 Provider 独立熔断**：每个 Client 实例绑定独立 `CircuitBreaker`，避免一个 Provider 拖垮全部
- **[P2] [C:⭐ I:⭐⭐⭐] Retry-After 与 jitter**：解析 429 响应头，退避加随机抖动，减少 thundering herd
- **[P2] [C:⭐ I:⭐⭐⭐] CLI `--timeout` / `--max-retries`**：透传到 `AnthropicCompatibleModelClient`
- **[P2] [C:⭐ I:⭐⭐⭐] FakeClient 支持 chat_with_tools**：单元测试 native tool 路径无需 mock HTTP
- **[P2] [C:⭐ I:⭐⭐⭐] bootstrap Provider 注册表**：`create_model_client` 统一支持 `openai`/`ollama`/`deepseek`，消除 CLI 与 bootstrap 双轨装配
- **[P3] [C:⭐⭐ I:⭐⭐] HTTP keep-alive**：同 session 多轮复用连接，降低 latency

---

## 4. CLI / REPL — cli.py

- **[P1] [C:⭐ I:⭐⭐⭐] 命令历史**：集成 `readline`，↑↓ 浏览历史、Ctrl-R 搜索
- **[P1] [C:⭐ I:⭐⭐⭐] `/memory` 真实输出**：当前 REPL 仍为占位文案，应渲染 working/episodic/durable 摘要
- **[P1] [C:⭐ I:⭐⭐⭐] `/quota` 与 reset**：显示 `QuotaEnforcer.status()`，支持 `/quota reset` 清零计数
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Tab 自动补全**：工具名 / 路径 / `/` 命令
- **[P2] [C:⭐ I:⭐⭐] 多行输入**：`\` 续行符支持长 prompt
- **[P2] [C:⭐⭐ I:⭐⭐⭐] /save /load 会话**：导出/恢复 session JSON 到指定路径，跨机器迁移
- **[P2] [C:⭐ I:⭐⭐⭐] `/runs` / `/replay`**：列出 `.agent/runs/`，对选定 trace 调用 `ReplayRunner`
- **[P2] [C:⭐ I:⭐⭐⭐] `/prompt` 调试**：打印下一帧 `ContextManager.build` 的 token 分段与 metadata
- **[P2] [C:⭐ I:⭐⭐⭐] `--verbose`**：打开 `_log_loop` 与 prompt 摘要，默认静默
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] REST API 模式**：`--serve :8000` 启动 HTTP，POST `/ask` + GET `/session/{id}`
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ `--health` 增强**：在现有 JSON 上增加 provider ping（可选 `--ping-api`）与 CB 状态
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ `--profile` 文档化**：dev/prod/ci 预设写入 `--help` 与 README，prod 显式为默认行为
- **[P3] [C:⭐⭐ I:⭐⭐] Markdown 渲染**：REPL 中代码块语法高亮（rich/pygments 可选依赖）

---

## 5. 进度回调 — callbacks.py

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 补全 AgentLoop 回调触发**：`on_step_start` / `on_final_answer` 已在 Protocol 定义但未调用，应在每轮 loop 首尾 invoke
- **[P1] [C:⭐ I:⭐⭐⭐] native tool 路径统一回调**：`_run_with_native_tools` 结束时应 `on_final_answer`
- **[P2] [C:⭐ I:⭐⭐⭐] JSONLinesCallback**：每事件一行 JSON 写 stderr，供外部进程 pipe 解析
- **[P2] [C:⭐ I:⭐⭐] NullCallback / CompositeCallback**：批量测试静默；多 listener 链式组合
- **[P2] [C:⭐ I:⭐⭐⭐] on_approval_required**：审批 Gate 触发前回调，TUI/Web UI 可接管 `input()`
- **[P2] [C:⭐ I:⭐⭐⭐] on_llm_start / on_llm_end**：携带 token_meta、latency，补全可观测性
- **[P3] [C:⭐⭐ I:⭐⭐] RichProgressCallback**：进度条 + spinner（optional `[dev]` extra）

---

## 6. 熔断与回放 — circuit_breaker.py / replay.py

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ `/session` CB 状态**：已实现 OPEN/HALF_OPEN/CLOSED；可增强为 JSON 片段供脚本解析
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] replay 完整参数回放**：`tool_executed` payload 写入 `tool`+`args`（经 redact），ReplayRunner 真正 re-exec 对比
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 回归检测**：CI 中 `--replay latest` 回放最近 trace，任一 diff 非零 exit 1
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 可视化 diff**：replay 差异以 unified diff 格式输出
- **[P2] [C:⭐ I:⭐⭐⭐] CB half-open 探测策略可配置**：探测成功 N 次才 closed，失败立即 reopen
- **[P3] [C:⭐⭐ I:⭐⭐] 熔断事件 trace**：状态切换时发射 `circuit_opened`/`circuit_closed` 事件

---

## 7. 持久化与恢复 — task_state / stores / checkpoint

- **[P1] [C:⭐⭐ I:⭐⭐⭐] run 自动清理**：`.agent/runs/` 无限增长，`--max-runs 100` 按 mtime 删旧
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] checkpoint 每轮创建**：当前仅 `ask_end`，应在每轮工具执行后 `trigger=step_end` 便于 crash 恢复
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 自动补做缺失步骤**：partial-stale 时根据 checkpoint `next_step` 提示模型重读文件
- **[P2] [C:⭐⭐ I:⭐⭐⭐] trace.jsonl 压缩**：单 run 超 1000 行自动 gzip 归档
- **[P2] [C:⭐ I:⭐⭐⭐] run_id 改用 UUID**：timestamp 格式易并发碰撞，改用 `uuid4().hex[:12]`
- **[P2] [C:⭐ I:⭐⭐⭐] report.json schema_version**：字段演进时 replay/CI 可校验版本
- **[P2] [C:⭐ I:⭐⭐] SessionStore 损坏恢复**：JSON 解析失败时读 `.bak` 或跳过并告警
- **[P2] [C:⭐ I:⭐⭐⭐] `/sessions` 列表**：REPL 列出所有 session id + mtime，支持 `/load <id>`

---

## 8. 上下文工程 — TokenBudget / ContextManager / 压缩 / 摘要

- **[P1] [C:⭐ I:⭐⭐⭐] 多模型 tokenizer 切换**：根据 `config.model` 选 encoding，未知模型 fallback cl100k_base 并 warn
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] memory section 优先级排序**：超出预算时按 task > modified > read > old summaries 取舍
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 摘要质量评估**：记录压缩比和关键实体保留率，差时自动放宽 BUDGET_HISTORY
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 摘要触发优化**：除 token 外，history 轮数 >10 也触发压缩
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量压缩**：在上一轮压缩结果上追加新条目，避免全量重摘要
- **[P2] [C:⭐ I:⭐⭐⭐] build() metadata 暴露**：`/session` 或 trace 展示各 section token 占用
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 动态预算分配**：简单任务收紧 TOTAL_BUDGET，多工具任务放宽
- **[P2] [C:⭐ I:⭐⭐⭐] section 预算 CLI 覆盖**：`--budget-history 3000` 等，写入 AgentConfig
- **[P2] [C:⭐ I:⭐⭐⭐] 工具结果结构化截断**：除字符截断外，按 token 数 fit 进 history

---

## 9. Prompt 工程 — prompt_prefix.py / context_manager.py

- **[P1] [C:⭐ I:⭐⭐⭐⭐] Cache 命中率统计**：`/session` 显示 `cache_hits / cache_misses`（需 Client 回传 cache 字段）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] few-shot 示例从文件加载**：`.agent/examples.md` → 替换硬编码 `TOOL_EXAMPLES`
- **[P2] [C:⭐ I:⭐⭐⭐] rules 外置**：`_rules()` 迁至 `.agent/rules.md`，支持项目定制审批说明
- **[P2] [C:⭐ I:⭐⭐] persona 可注入**：`Agent(system_prompt=...)` 与默认 persona 合并策略文档化
- **[P2] [C:⭐ I:⭐⭐⭐] prefix 失效检测**：tools 注册变更时自动 rebuild `_prefix`，避免 stale tool_signature

---

## 10. 模型输出解析 — runtime.py

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 批量工具调用**：单次响应多个 `<invoke>` / JSON array 全部解析执行
- **[P2] [C:⭐ I:⭐⭐] 解析错误精确定位**：retry 时告知 tag/offset 与期望格式片段
- **[P2] [C:⭐ I:⭐⭐⭐] XML 属性格式补全**：`<tool name="x" path="f">` 分支应映射到标准 args，当前仅返回 attrs/body
- **[P3] [C:⭐⭐ I:⭐⭐⭐] 思考链 (CoT) 提取**：从 thinking / reasoning 块剥离后再进 history

---

## 11. 控制循环 — agent_loop.py

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步工具超时**：每个 `execute_tool` 包 `concurrent.futures` 或 signal 超时，防 hang
- **[P1] [C:⭐⭐⭐⭐ I:⭐⭐⭐⭐] 流式输出支持**：文本解析路径逐 chunk 解析 `<tool>` / `<final>`，不等完整响应
- **[P1] [C:⭐⭐ I:⭐⭐⭐] ✅ 解析 retry 指数退避**：已实现；可配置 `--retry-max-delay`
- **[P2] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐] 并行工具执行**：同一响应解析出多个无冲突 tool 时并行 run
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 双路径行为对齐**：native `chat_with_tools` 与 text parsing 应产出相同 trace 事件集与 checkpoint 节奏
- **[P2] [C:⭐ I:⭐⭐⭐] stop_reason 枚举化**：`final|step_limit|circuit_breaker|parse_fail` 写入 report 供 eval 统计
- **[P2] [C:⭐ I:⭐⭐] `_finalize_run` 错误可见**：当前 broad except 吞异常，至少 stderr 告警 + trace `finalize_error`

---

## 12. 配置系统 — config.py

- **[P2] [C:⭐⭐ I:⭐⭐⭐] ✅ `--profile` 扩展**：增加 `test` profile（fake provider + 低 max_steps）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 热重载**：REPL `/config max_steps=10` 动态改字段并 invalidate checkpoint identity
- **[P2] [C:⭐ I:⭐⭐⭐] AgentConfig 补全字段**：`total_token_budget`、`tool_timeout`、`semantic_memory_enabled` 进 pydantic 模型
- **[P2] [C:⭐ I:⭐⭐] env 前缀统一**：`AGENT_RUNTIME_MAX_STEPS` 等与 `.env` 文档对齐

---

## 13. 记忆系统 — features/memory/

### 13.1 工作记忆

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] ✅ task_summary 模型生成**：`_gen_task_summary` 已用 light_client；可增强失败率 metric
- **[P2] [C:⭐ I:⭐⭐⭐] recent_files 带访问时间戳**：`/memory` 按时间排序展示
- **[P2] [C:⭐ I:⭐⭐⭐] file_summaries TTL 自动过期**：30 分钟未读清理，防堆积

### 13.2 事件记忆

- **[P2] [C:⭐⭐ I:⭐⭐⭐] episodic → durable 自动晋升**：kind="decision" 且多次被检索的笔记 promote

### 13.3 持久记忆

- **[P1] [C:⭐⭐ I:⭐⭐⭐] 条目带时间戳 + 自动归档**：超 N 天未检索移到 archive
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 冲突检测与合并**：同 topic 相似条目提示合并

### 13.4 语义记忆

- **[P2] [C:⭐ I:⭐⭐⭐] embedding 缓存**：相同文本不重复 encode
- **[P1] [C:⭐ I:⭐⭐⭐] 语义模型懒加载开关**：`--no-semantic` 跳过 `_get_semantic_model`，加速 CI/health

### 13.5 整体

- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 记忆优先级仲裁**：超出 800 token 时按 task > files > summaries 取舍
- **[P2] [C:⭐ I:⭐⭐⭐] memory export/import**：JSON 导出 `.agent/memory.json` 便于调试
- **[P3] [C:⭐⭐⭐ I:⭐⭐⭐] 上下文感知记忆**：根据任务类型（读代码 vs 改配置）调整检索权重

---

## 14. 工作区上下文 — workspace.py / tool_context.py

- **[P1] [C:⭐ I:⭐⭐⭐] Workspace 增量刷新**：每轮 ask 不全量 `git status`，仅 fingerprint 变化时重建
- **[P2] [C:⭐ I:⭐⭐⭐] DOC_NAMES 可配置**：env `AGENT_RUNTIME_DOCS=AGENTS.md,CUSTOM.md` 扩展白名单
- **[P2] [C:⭐ I:⭐⭐] git 输出截断**：`git status` / `log` 超 30 行截断并提示用工具深入
- **[P2] [C:⭐ I:⭐⭐⭐] 非 git 工作区降级**：无 `.git` 时仍提供 cwd + 文档，不静默空 branch
- **[P2] [C:⭐ I:⭐⭐] ToolContext read_only 模式**：只读 Agent 禁止 resolve 到即将写入的路径外

---

## 15. Bootstrap 与公开 API — bootstrap.py / __init__.py

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 稳定公开导出**：`agent_runtime.__all__` 扩展 `create_model_client`、`AgentLoop`、`ToolExecutor`、`SessionStore` 等 L2 集成面
- **[P2] [C:⭐ I:⭐⭐⭐] bootstrap 单测契约**：保证 `load_dotenv` 不覆盖已有 env、fake provider 行为不变
- **[P2] [C:⭐ I:⭐⭐] 版本号**：`agent_runtime.__version__` 与 pyproject 同步，health JSON 输出

---

## 16. TaskState 与可观测性 — task_state.py / run_store.py

- **[P2] [C:⭐ I:⭐⭐⭐] node_timings 扩展**：区分 `model_call_ms` 首次 vs retry 累计
- **[P2] [C:⭐ I:⭐⭐⭐] token_usage 与 Client 对齐**：report 写入 `session_usage` 总量，不只 context metadata
- **[P2] [C:⭐ I:⭐⭐] 导出 run 包**：`fixloop export-run <run_id>` zip task_state + trace + report
- **[P2] [C:⭐ I:⭐⭐⭐] trace 事件 schema**：`run_started|tool_executed|run_finished` payload 字段文档 + 校验测试

---

## 17. 整体增强（L1 范围）

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一日志模块**：`agent_runtime.logger` 替代散落 `stderr.write`，支持 `--log-level`
- **[P1] [C:⭐ I:⭐⭐⭐] 启动自检 `--check`**：Python/tiktoken/rg/git/.env 完整性（`--health` 侧重运行态）
- **[P2] [C:⭐ I:⭐⭐⭐] 运行时注册工具**：`agent.register_tool(name, spec)` 动态扩展 registry 并重算 prefix
- **[P2] [C:⭐⭐⭐⭐ I:⭐⭐⭐] WebSocket 推送**：`--serve` 模式下 WS 广播 callback 事件
- **[P2] [C:⭐ I:⭐⭐⭐] Agent 可中断**：SIGINT 安全停机，写 partial checkpoint + `status=interrupted`
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐⭐] 多 Agent 同进程**：共享 semantic model 单例前提下隔离 session/quota

---

*文档版本：Layer 1 Bonus · 独立探索 · base `master` @ PR #83*
