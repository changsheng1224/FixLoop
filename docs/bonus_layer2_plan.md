# Layer 2 可改进与可额外实现功能探索

> 覆盖 `src/` 多 Agent 修复系统。评测见 [`bonus_m7-m8.md`](bonus_m7-m8.md)；Layer 1 见 [`bonus_layer1_plan.md`](bonus_layer1_plan.md)。  
> 基线：`master` @ PR #83（Design Patterns A–G 已合入）。

---

## 1. Orchestrator 编排 — `orchestrator.py` / `repair/pipeline.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Prompt 模块外置**：将 `_localizer/_retriever/_patcher_prompt` 迁入 `repair/prompts.py`，由 `PromptBuilder(state, repo_root)` 组装字符串，Orchestrator 只负责调用。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Issue 解析外置**：将 `_parse_issue`、堆栈行号提取、Skill 匹配迁入 `repair/issue_parser.py`，表驱动单测覆盖各 `issue_type`。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 分阶段超时**：在 `repair()` 入口增加 localize / patcher / verify 独立 timeout，ThreadPool 或 `future.result(timeout=)` 分别包装。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 反馈环增强**：`_build_feedback` 合并失败测试名、上轮修改文件列表、回滚提示、`build_log` 摘要后再写入 `state.feedback`。
- **[P2] [C:⭐ I:⭐⭐⭐] 测试发现外置**：`_pick_test_path` 等逻辑迁入 `repair/test_discovery.py`，与 Retriever 工具规则共用。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] JSON 失败自动重试**：suspects/patches 为空时追加「仅输出合法 JSON」短 prompt，对同一 Agent 再 `ask()` 一次。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Pipeline 钩子**：`RepairPipelineMixin` 支持注册 `before_patcher` / `after_verify` 回调，扩展 Critic 或日志无需子类化。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] OrchestratorDeps 注入**：用 dataclass/Protocol 封装四 Agent + 可选 `VerifyStrategy`，测试替换 Fake 依赖。
- **[P2] [C:⭐ I:⭐⭐] 删解析薄包装**：去掉 `_parse_suspect_list` 等 delegate，pipeline 直接调用 `output_parsers`。
- **[P3] [C:⭐⭐⭐ I:⭐⭐⭐] Critic Agent**：只读 `RepairState` 在 Patcher 前输出「嫌疑是否充分」结论，不写入 repo。
- **[P3] [C:⭐⭐ I:⭐⭐] orchestrator 包化**：单文件仍 >500 行时再拆 `src/orchestrator/` 子模块 + re-export。

---

## 2. 状态与 Blackboard — `state.py` / `blackboard.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] schema 版本迁移**：`from_dict` 按 `schema_version` 分支升级字段，保证 eval trace 可读。
- **[P2] [C:⭐ I:⭐⭐⭐] 上下文字段进 Prompt**：`caller_locations` / `similar_fixes` 在 Patcher prompt 独立段落输出，空则省略。
- **[P2] [C:⭐ I:⭐⭐⭐] agent_errors 结构化**：改为 `(agent, code, message)` 列表，便于统计 JSON 解析失败率。
- **[P2] [C:⭐ I:⭐⭐⭐] Blackboard 轻量接入**：L/R 完成后写入 suspect/context 摘要；冲突计数进 `node_timings`。
- **[P2] [C:⭐ I:⭐⭐] 冲突真正仲裁**：`resolve_conflict()` 保留 `winner_source` 对应 value，而非仅删记录。
- **[P2] [C:⭐ I:⭐⭐⭐] Repair 运行落盘**：每次 `repair()` 结束写 `.agent/repairs/{id}/repair_state.json` 与 timings。
- **[P3] [C:⭐⭐ I:⭐⭐] Agent 直写 Blackboard**：仅多 Writer / 双 Localizer 实验时需要。

---

## 3. Skill 与记忆 — `skills/*.yaml` + Layer 1 Memory

- **[P1] [C:⭐ I:⭐⭐⭐] Skill 注入 Prompt**：匹配 YAML 后将 `example_patch` / `suggested_tools` 写入 `[Skill 提示]` 段，不只写 `estimated_impact`。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] similar_fixes 检索**：Retriever 调用 SemanticMemory / episodic，按 issue 类型与文件路径填充历史修复摘要。
- **[P2] [C:⭐ I:⭐⭐] Skill 匹配优先级**：YAML 增 `priority` 或最长 pattern 优先，避免泛化 skill 覆盖专用 skill。
- **[P2] [C:⭐ I:⭐⭐⭐] matched_skill 指标**：命中 skill 名写入 `node_timings`，eval 可聚合命中率。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 修复结果写回记忆**：`status=fixed` 时追加 durable 笔记（issue 摘要 + diff 路径）。
- **[P3] [C:⭐⭐ I:⭐⭐] Skill 热加载**：启动或定时扫描 `skills/` mtime 重载，无需重启进程。

---

## 4. 多 Agent 工厂 — `agents/factory.py` / `prompts/`

- **[P1] [C:⭐ I:⭐⭐⭐] 角色类型统一**：`RepairAgentRole` 在 factory 与 composite 单一定义并 export，避免 Literal 漂移。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Prompt A/B 记录**：新增 `docs/prompt-ab-results.md`，记录各 Agent 变体的 JSON 解析率与 Fix 率。
- **[P2] [C:⭐ I:⭐⭐⭐] 装配逻辑收敛**：`wire_orchestrator` 迁入 `repair/wiring.py` 或紧挨 `repair_factory`，边界更清晰。
- **[P2] [C:⭐ I:⭐⭐⭐] Prompt 版本追踪**：`prompts/*.txt` 头行 `# version: N`，写入 trace 便于回归对比。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 失败驱动 Prompt 片段**：从 `agent_errors` 聚合高频错误，自动生成「禁止事项」追加段。
- **[P2] [C:⭐ I:⭐⭐] Agent 配置外置**：`max_steps` / `max_new_tokens` 可来自 YAML 或 env 覆盖默认值。
- **[P2] [C:⭐ I:⭐⭐] Prompt 快照单测**：断言生成的 user prompt 含 JSON schema 关键句。

---

## 5. Patcher 与 Verifier 策略 — `repair/patch_applier.py` / `repair/verify.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 应用前磁盘校验**：`original_lines` 与文件内容不一致时拒绝应用并写入 feedback。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 补丁上限强制**：对齐 `MAX_PATCHES` / `MAX_LINES`，超限拒绝并记 `agent_errors`。
- **[P1] [C:⭐ I:⭐⭐⭐] 验证范围开关**：CLI `--verify-scope related|all`，pytest 只跑相关测试或全量。
- **[P2] [C:⭐ I:⭐⭐⭐⭐] 双 PatchApplier 重命名**：harness 侧改 `ContainerPatchApplier`，repair 侧改 `RepoPatchApplier`，文档区分宿主机 vs 容器。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Patcher 模式切换**：环境变量或 CLI 选择 `direct`（`complete_once`）与 `loop`（Agent + patch_file）。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Best-of-N 补丁**：一次解析多个 `CandidatePatch`，按序 apply + verify 直到通过。
- **[P2] [C:⭐ I:⭐⭐] Verifier 模式切换**：`--verifier-mode harness|agent`；agent 模式走 Verifier Agent + sandbox 工具。
- **[P2] [C:⭐ I:⭐⭐⭐] build_log 进反馈**：Docker build 失败时将 stderr 前 N 行并入 `_build_feedback`。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 容器 lint**：验证阶段可选跑 `ruff check`，结果写入 `VerificationResult.lint_issues`。
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 容器内打补丁**：`--patch-target container` 时仅改容器 `/code`，验证通过后 `docker cp` 导出 diff。
- **[P2] [C:⭐ I:⭐⭐⭐] repair 公开 API**：在 `repair/__init__.py` export 稳定符号供外部与文档引用。

---

## 6. Docker Harness — `harness/` / `tools/sandbox_tools.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 镜像预热检查**：repair / demo 启动前 `docker image inspect`，缺失则打印 build 指引。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 温容器池**：复用 `sleep infinity` 容器，每轮增量 tar 同步变更，verify 结束可选择性 reset。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] tar 增量打包**：按 mtime/size 只传变更文件，降低大 repo 传输耗时。
- **[P2] [C:⭐ I:⭐⭐] 池化隔离**：每轮 verify 前快照容器 FS 或换容器 ID，避免 patch 交叉污染。
- **[P2] [C:⭐ I:⭐⭐⭐] pytest 超时兜底**：无 JSON 报告且 `exit_code=-1` 时仍生成明确 `failure_logs`。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 多语言沙箱 profile**：`SandboxManager.create(profile="node")` + 对应 Dockerfile 占位。

---

## 7. 工具与权限 — `tools/` / `middleware.py`

- **[P1] [C:⭐ I:⭐⭐⭐] Retriever 规则快路径**：`--fast-retrieve` 跳过 LLM，用 `find_test` + `read_file` 填 `RetrievedContext`。
- **[P2] [C:⭐ I:⭐⭐⭐] Localizer 工具顺序约束**：Prompt 要求先 `stack_parse` 再 `ast_parse`；违规写 stderr 警告。
- **[P2] [C:⭐ I:⭐⭐⭐] 越权审计**：`permission_denied` 写入 trace 或 `agent_errors`，便于演示 ToolGateway。
- **[P2] [C:⭐ I:⭐⭐] 权限表外置**：`REPAIR_PERMISSION_TABLE` 迁至 YAML，启动时加载到 `ToolGateway`。
- **[P2] [C:⭐ I:⭐⭐] Registry Builder**：合并 `registry.py` 重复字面量为小型 builder 函数。
- **[P2] [C:⭐ I:⭐⭐⭐] ast_parse 局部解析**：大文件仅解析 suspect 行附近 AST，降低工具耗时。
- **[P3] [C:⭐⭐ I:⭐⭐] 多语言工具 stub**：为 Node/Java 预留 `build_*_tools(ctx)` 接口。

---

## 8. CLI 与装配 — `cli.py` / `repair_factory.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] repair 退出码**：`0=成功`，`1=失败/耗尽`，`2=配置错误`，`3=超时`；`_repair` 按 `state.status` 映射。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] repair --json**：stdout 输出 `RepairState` 序列化 JSON，stderr 保留 verbose。
- **[P2] [C:⭐ I:⭐⭐⭐] CLI 暴露重试与超时**：`--max-retries` / `--timeout` 传入 Orchestrator。
- **[P2] [C:⭐ I:⭐⭐⭐] repair doctor**：子命令检查 API key、Docker、镜像、cases 完整性。
- **[P2] [C:⭐ I:⭐⭐⭐] dry-run 贯穿 Verifier**：dry-run 时注入 NoOp `VerifyStrategy` 或跳过 Docker。
- **[P2] [C:⭐ I:⭐⭐⭐] Verifier 创建可观测**：verbose 时说明 Docker 不可用而 fallback pytest。
- **[P2] [C:⭐ I:⭐⭐⭐] run_pytest 上移**：从 `eval/runner` 迁到 `harness` 或 `repair`，消除 verify→eval 依赖。

---

## 9. 文档与边界 — ARCHITECTURE / ADR

- **[P1] [C:⭐ I:⭐⭐⭐⭐] ARCHITECTURE 同步**：更新 Layer 2 协作图（`tool_policy`、`src/repair/` 模块），删「wrap_agent」表述。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] L1-L2 公开 export**：完成 Phase 5–6，`agent_runtime.public` 与 `src.repair` 稳定导出清单。
- **[P2] [C:⭐ I:⭐⭐⭐] ADR 补全**：宿主机 vs 容器 PatchApplier、Fix Rate vs 效率叙事（ADR-011）。
- **[P2] [C:⭐ I:⭐⭐] Blackboard Phase H**：文档标注 ADR-004 主路径为 RepairState，Blackboard 为扩展面。

---

## 10. 测试补强

- **[P1] [C:⭐ I:⭐⭐⭐] CLI 退出码单测**：mock `status=failed` 断言 `main()!=0`。
- **[P1] [C:⭐ I:⭐⭐⭐] issue_parser 表驱动**：每种 `issue_type` 一条输入/期望 `RepairPlan`。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Docker 集成测**：`@pytest.mark.docker` 验证 sandbox verify 路径，无 Docker skip。
- **[P2] [C:⭐ I:⭐⭐⭐] Blackboard 集成测**：双 source 写同 key → 冲突 → 仲裁。
- **[P2] [C:⭐ I:⭐⭐] Skill 命中单测**：固定 issue 文本断言匹配 yaml。

---
