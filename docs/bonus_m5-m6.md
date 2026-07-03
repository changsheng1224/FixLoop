# M5–M6 可改进与可额外实现功能探索

> 对照 `docs/M5-M6-DAILY.md`、`M5_GUIDE.md`、`M6_GUIDE.md` 与当前实现（`master` @ M6D5，`376 tests`）整理。  
> 优先级：**P1** 高收益/低成本 · **P2** 中等 · **P3** 锦上添花。  
> 标注：**[C:⭐…]** 实现复杂度 · **[I:⭐…]** 价值/面试/评测收益。

---

## 0. 计划 vs 实现：缺口速览

| 计划项 | 当前状态 | 建议 |
|--------|----------|------|
| Blackboard 接入 Orchestrator | `src/blackboard.py` 有单测，流水线用 `RepairState` 直传 | 见 §1.1；非运行依赖，文档/叙事需要时再轻量接 |
| `docs/prompt-ab-results.md` | 未创建 | M7 前补 1 页 A/B 记录或 ADR 引用 |
| `test_self_healing.py` 独立文件 | 逻辑并入 `test_e2e_repair.py` | 可接受；可选拆回独立文件便于检索 |
| Verifier LLM Agent loop | Orchestrator 直连 `run_sandbox_verification()` | 设计选择（省 token/延迟）；见 §3.3 |
| `PatchApplier` 容器内打补丁 | 仅单测；实际在**宿主机**落盘再 tar 进容器 | 见 §2.3（真隔离 vs 当前简化路径） |
| 镜像预 pull / 沙箱连接池 | Day 29 延后 | 见 §2.1 |
| `asyncio.gather` 并行 L∥R | 已实现 `ThreadPoolExecutor` 并行 | 等价够用；async 化见 §4.1 |
| CLI `repair` 失败退出码 | 始终 `return 0` | 见 §5.1 |
| Skill `suggested_tools` 注入 | 写入 `RepairPlan.estimated_impact`，对 Prompt 影响弱 | 见 §1.4 |
| `RetrievedContext.similar_fixes` 等字段 | 模型可输出，Orchestrator 未专门消费 | 见 §1.3 |

---

## 1. 多 Agent 架构与状态

### 1.1 Blackboard

- **[P2] [C:⭐ I:⭐⭐⭐] Orchestrator 轻量接入 Blackboard**：L/R 完成后 `write("suspect:…")` / `write("context:tests")`；`snapshot()` 挂到 `RepairState` 或 trace；冲突时 `_merge_suspects()` 取最高 `confidence`。Agent 仍不经 Blackboard 直写。
- **[P3] [C:⭐⭐ I:⭐⭐] Agent 直写 Blackboard**：需改 Agent loop 回调，收益仅在多路并行定位（Critic、双 Localizer）时明显。
- **[P2] [C:⭐ I:⭐⭐] `resolve_conflict()` 补全实现**：当前只删 conflict 记录，未真正保留 `winner_source` 对应 value。
- **[P2] [C:⭐ I:⭐⭐⭐] Blackboard snapshot 写入 trace**：`.agent/repair/{run_id}/blackboard.json`，M7 消融可统计冲突率。

### 1.2 RepairState / 协议

- **[P1] [C:⭐ I:⭐⭐⭐⭐] `schema_version` 迁移链**：`from_dict` 按版本分支（如 `1.0→1.1` 字段重命名），避免评测集 JSON 断裂。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Repair 运行持久化**：每次 `repair()` 结束写 `repair_state.json` + `node_timings`，支持 `--resume` 从 Patcher 轮次继续。
- **[P2] [C:⭐ I:⭐⭐⭐] `RepairState.to_json()` CLI 输出**：`repair --json` 供 CI/评测脚本解析，不依赖 stdout 中文关键字。
- **[P2] [C:⭐ I:⭐⭐] 字段利用率**：`caller_locations` / `similar_fixes` 在 Patcher prompt 中显式分段；空字段不占位。

### 1.3 Layer 1 记忆 × Layer 2 修复

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Retriever 检索历史修复**：用 `SemanticMemory` / episodic 填 `similar_fixes`（同 repo 曾修过的 `TypeError@calc.py`）。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 修复结果写回持久记忆**：`status=fixed` 时追加 durable 笔记（issue 摘要 + patch diff），下轮同类 issue 提速。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] Localizer 用 semantic 搜相似堆栈**：非 git 仓库或堆栈含糊时的降级定位。

### 1.4 Skill 系统

- **[P1] [C:⭐ I:⭐⭐⭐] Skill 注入 Patcher/Localizer prompt**：除 `estimated_impact` 外，把 `example_patch` / `suggested_tools` 写入 user prompt 段落 `[Skill 提示]`。
- **[P2] [C:⭐ I:⭐⭐] Skill 匹配优先级**：多 pattern 命中时按 `priority` 字段或最长匹配排序，避免泛化 `TypeError` 盖过专用 skill。
- **[P2] [C:⭐ I:⭐⭐⭐] Skill 命中率统计**：Orchestrator 记录 `matched_skill` 到 `node_timings`，M7 表格列「skill 是否命中」。
- **[P3] [C:⭐⭐ I:⭐⭐] 热加载 Skill YAML**：`skills/*.yaml` 变更无需重启进程。

---

## 2. Docker 沙箱与验证

### 2.1 性能与资源

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 镜像预热 / 存在性检查**：CLI 启动或 `demo_repair.sh` 前 `docker image inspect`；缺失则提示 build 命令，避免首次 repair 卡在 pull。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 沙箱连接池 / 温容器**：复用 `sleep infinity` 容器，仅 tar 增量同步变更文件；预期每轮 Verifier 省 0.5–1s（3 轮自愈更明显）。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] tar 增量传输**：对比 mtime/size 只打包变更文件，大 repo 降 tar 耗时。
- **[P2] [C:⭐ I:⭐⭐] 并行自愈时容器隔离**：池化时需保证每 turn 文件系统快照隔离，避免 patch 交叉污染。

### 2.2 验证语义

- **[P1] [C:⭐ I:⭐⭐⭐⭐] Verifier 全量 vs 相关用例开关**：`--verify-scope related|all`；默认 `related` 保速度，CI 用 `all` 防漏测。
- **[P1] [C:⭐ I:⭐⭐⭐] `build_log` 写入反馈**：pip 失败时 `_build_feedback()` 附带 `build_log` 前 N 行，Patcher 可修 `pyproject.toml` 类问题。
- **[P2] [C:⭐ I:⭐⭐⭐] pytest 超时路径强化**：`exit_code=-1` 且无 JSON 报告时，`failed≥1` + 明确 `failure_logs`（当前可能 `failed=0` 但 `all_passed=False`）。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 容器内 lint**：`ruff check` 结果并入 `VerificationResult.lint_issues`，类型/风格问题进反馈环。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 多语言 profile**：`SandboxManager.create(profile="node")` + 对应 Dockerfile（M7+ 扩展）。

### 2.3 补丁应用路径

- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 真·容器内 PatchApplier**：补丁只写入容器 `/code`，**宿主机保持 bug 状态**；演示 repo 无需 `git checkout` 恢复。与当前「宿主机改 + tar 验证」二选一或 `--patch-target host|container`。
- **[P2] [C:⭐ I:⭐⭐] 验证通过后一次性导出补丁**：容器内修复成功 → `docker cp` diff 回宿主机 → 可选 `git apply`。
- **[P1] [C:⭐ I:⭐⭐⭐] `PatchApplier` 接入 E2E 单测**：真实 docker fixture（标记 `@pytest.mark.docker`）验证容器内 apply + revert。

---

## 3. Agent 与 Prompt

### 3.1 Localizer / Retriever

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] JSON 解析失败自动重试**：解析 0 suspects 时 Orchestrator 追加「仅输出合法 JSON」再 `ask()` 一次（已有 stderr 日志，缺自动重试）。
- **[P2] [C:⭐ I:⭐⭐⭐] 工具调用轨迹约束**：Localizer 必须先 `stack_parse` 再 `ast_parse` 的 prompt 硬约束 + 违规时 Orchestrator 警告。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Retriever 无 LLM 快速路径**：规则引擎：`find_test` + `read_file` 嫌疑行上下文，零 API 调用模式（`--fast-retrieve`）。
- **[P3] [C:⭐⭐ I:⭐⭐] 多文件嫌疑排序**：按 `confidence` + 堆栈深度排序后截断 top-K 进 Patcher，控 token。

### 3.2 Patcher

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 统一 diff 输出校验**：应用前检查 `original_lines` 与磁盘一致，不一致时拒绝并反馈「行内容已变」。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Patcher 多候选补丁**：一次 API 返回 N 个 `CandidatePatch`，逐个验证（best-of-N），提高复杂 bug 成功率。
- **[P2] [C:⭐ I:⭐⭐⭐] 恢复 Agent loop 模式（可配置）**：`PATCHER_MODE=direct|loop`；换用遵守 tool 格式的模型时可切回 loop + `patch_file`。
- **[P3] [C:⭐⭐ I:⭐⭐] unified diff 原生支持**：Patcher 输出标准 unified diff，由 `patch` 命令应用。

### 3.3 Verifier

- **[P2] [C:⭐ I:⭐⭐] Verifier LLM 可选路径**：`--verifier-mode harness|agent`；默认 harness；调试 sandbox 工具时用 agent。
- **[P2] [C:⭐ I:⭐⭐⭐] 验证结果结构化进 trace**：`sandbox_timings` + `failure_logs` 完整写入 JSONL，不只 stderr 打印。

### 3.4 Prompt 工程

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 补 `docs/prompt-ab-results.md`**：4 Agent 各 1 个变体 × 3 case × 3 次，记录 JSON 解析率、耗时、Fix/NotFix。
- **[P2] [C:⭐ I:⭐⭐⭐] Prompt 版本号**：`prompts/*.txt` 顶行 `# version: 2`，写入 trace 便于回归对比。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 失败驱动的 prompt 片段**：从 `agent_errors` 统计高频错误，自动生成「禁止事项」追加段。

---

## 4. Orchestrator 编排

### 4.1 并发与超时

- **[P2] [C:⭐⭐ I:⭐⭐] 原生 asyncio 流水线**：`asyncio.gather` + async docker（若上游支持），与 Layer 1 异步风格统一；当前 `ThreadPoolExecutor` 可保留。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 分阶段超时**：`localize_timeout_s` / `patcher_timeout_s` / `verify_timeout_s` 独立配置，避免 Localizer 吃满 180s。
- **[P2] [C:⭐ I:⭐⭐⭐] 可配置 `max_retries`**：CLI `--max-retries N`，与 `RepairState.max_retries` 对齐。

### 4.2 降级与自愈

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 反馈环 enriched**：除失败测试名外，附带「上次改了什么文件/行」+「回滚已执行」，减少 Patcher 重复犯错。
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] 第 5 个 Agent：Critic**：只读 Blackboard，在 Patcher 前审查 suspect 是否充分；或 Patcher 后审查 patch 是否过度修改。
- **[P2] [C:⭐ I:⭐⭐⭐] Retriever 失败时 search 降级**：Orchestrator 用 `rg` 堆栈文件名直搜，不依赖 Retriever JSON。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 动态重试策略**：第 2 轮起放大检索范围 / 放宽修改文件白名单。

### 4.3 安全与边界

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 补丁行数/文件数上限**：对齐 `PatchApplier` 常量（`MAX_PATCHES`/`MAX_LINES`），超限拒绝并反馈。
- **[P2] [C:⭐ I:⭐⭐⭐] 二进制/生成文件黑名单**：拒绝 patch `*.pyc`、`.git/`、`uv.lock` 等。
- **[P2] [C:⭐ I:⭐⭐⭐] ToolGateway 审计**：越权尝试写入 `agent_errors` 或 trace 事件 `permission_denied`。

---

## 5. CLI 与可观测性

### 5.1 CLI

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 退出码语义**：`0=fixed`，`1=failed/exhausted`，`2=配置错误`，`3=超时`；`demo_repair.sh` 用 `$?` 判断。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] `repair --json`**：stdout 输出 `RepairState.to_dict()`，stderr 保留 verbose 日志。
- **[P2] [C:⭐ I:⭐⭐⭐] `--max-retries` / `--timeout`**：暴露 Orchestrator 参数。
- **[P2] [C:⭐ I:⭐⭐⭐] `repair --dry-run` 贯穿 Verifier**：当前仅 Agent `dry_run`；Verifier 应跳过 Docker 或 mock。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] `repair doctor`**：检查 API key、Docker、镜像、demo 依赖、`.env`。

### 5.2 Trace / 指标

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Repair 专用 trace**：`.agent/repairs/{id}/trace.jsonl` 记录每阶段输入输出摘要（redact 后）。
- **[P2] [C:⭐ I:⭐⭐⭐] 单次 repair 指标卡片**：Fix/NotFix、retry 次数、各阶段 ms、token 用量（若 client 暴露）。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 与 Layer 1 RunStore 打通**：一次 repair = 一个 run_id，可在 REPL `/replay` 中查看。

---

## 6. 测试与 Demo

### 6.1 测试缺口（M6D5 后仍可做）

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Docker 集成测试（可选 CI job）**：`@pytest.mark.docker` 跑 `demo/calculator` 沙箱验证路径；无 Docker 环境 skip。
- **[P1] [C:⭐ I:⭐⭐⭐] CLI 失败退出码单测**：mock `status=failed` 时 `main()!=0`。
- **[P2] [C:⭐ I:⭐⭐⭐] Blackboard × Orchestrator 集成测**：双 source 写同 key → 冲突 → 仲裁。
- **[P2] [C:⭐ I:⭐⭐] Skill 匹配单测**：各 demo issue 命中预期 yaml。
- **[P2] [C:⭐ I:⭐⭐⭐] `test_self_healing.py` 独立文件**：从 `test_e2e_repair` 拆出，对齐 M5-M6 文件清单。

### 6.2 Demo 扩展

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 第 4–10 个 demo case**：`KeyError`、`AttributeError`、异步 bug、多文件联动；对齐 M7 评测集种子。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 「需 2 轮自愈」专用 demo**：故意让首轮补丁 partial fix，用于演示 feedback 环。
- **[P2] [C:⭐ I:⭐⭐⭐] demo 内嵌 expected_patch 元数据**：`demo/*/case.yaml` 供自动评分，不泄露给 Agent prompt。
- **[P3] [C:⭐⭐ I:⭐⭐] 录制 asciinema / GIF**：附着 `demo_repair.sh` 文档。

---

## 7. 文档与面试叙事（M7 前置）

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] ADR：`docs/design-decisions.md`**：至少 ④ Blackboard vs RepairState、⑤ Skill YAML、⑥ 宿主机 patch vs 容器 patch、⑦ Verifier harness 直连。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] `ARCHITECTURE.md` Layer 2 章**：协作图与 M6_GUIDE §4 同步，标明 Blackboard 实际/设计双轨。
- **[P2] [C:⭐ I:⭐⭐⭐] 更新 M5_GUIDE 架构图**：Blackboard 标注为「可选/轻量接入」或「RepairState 等价实现」。
- **[P2] [C:⭐ I:⭐⭐] `prompt-ab-results.md`**：见 §3.4。

---

## 8. 推荐实施顺序（Bonus 路线图）

若时间有限，建议按下面三批做，与 M7 评测衔接最好：

| 批次 | 项 | 预期收益 |
|:--:|---|------|
| **A**（1–2 天） | CLI 退出码 + `--json`；分阶段超时；Verifier `--verify-scope`；`prompt-ab-results.md` | CI 可集成；文档闭环 |
| **B**（2–3 天） | 镜像预热 + 温容器池；反馈环 enriched；Skill 注入 prompt；Repair trace | 耗时降 20–30%；成功率提升 |
| **C**（3–5 天） | Blackboard 轻量接入 + 集成测；SemanticMemory → `similar_fixes`；demo case 扩至 6+ | 叙事对齐 M5；M7 评测种子 |

**不建议 M6 后立刻做**：Agent 直写 Blackboard、多语言沙箱、WebSocket 推送——复杂度高，M7 消融前收益不明显。

---

## 9. 相关文档

- 每日计划：`docs/M5-M6-DAILY.md`
- 完成态复盘：`docs/M6_GUIDE.md` §8、§10.4
- Layer 1 同类探索：`docs/bonus_layer1_plan.md`
- M7 评测：`docs/M7-M8-DAILY.md`
