# M5–M6 可改进与可额外实现功能探索

> 对照 M5–M6 计划与当前 Layer 2 实现。基线：`master` @ PR #83（`484 tests`）。  
> **已在 #83 完成**：ToolGateway→`tool_policy`、`VerifyStrategy`、`RepoPatchApplier`、`RepairPipelineMixin`、`output_parsers`、`repo_snapshot`、baseline factory — 下列未标注 ✅ 项仍为可做 Bonus。

---

## 1. 多 Agent 架构 — Orchestrator / Blackboard / RepairState

- **[P1] [C:⭐ I:⭐⭐⭐⭐] schema 版本迁移**：`RepairState.from_dict` 按版本分支，兼容旧 eval JSON。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Repair 运行持久化**：每次 repair 写 `repair_state.json` + timings，可选 `--resume` 从某 retry 继续。
- **[P2] [C:⭐ I:⭐⭐⭐] Blackboard 轻量接入**：L/R 后写入 suspect/context；`.agent/repair/{id}/blackboard.json` 供 trace。
- **[P2] [C:⭐ I:⭐⭐] resolve_conflict 补全**：冲突时保留 winner 的 value，不静默丢弃。
- **[P2] [C:⭐ I:⭐⭐⭐] 字段利用率**：Patcher prompt 显式消费 `caller_locations` / `similar_fixes`。
- **[P3] [C:⭐⭐ I:⭐⭐] Agent 直写 Blackboard**：多 Writer 实验时再改 Agent loop 回调。

---

## 2. Layer 1 记忆 × 修复 — Retriever / Localizer

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 历史修复检索**：SemanticMemory / episodic 填 `RetrievedContext.similar_fixes`。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 修复写回 durable**：`status=fixed` 时追加 issue 摘要 + patch 笔记。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 堆栈语义定位**：Localizer 用 semantic 搜相似堆栈，作无 git 降级。

---

## 3. Skill 系统 — `src/skills/*.yaml`

- **[P1] [C:⭐ I:⭐⭐⭐] Skill 注入 Prompt**：`example_patch` / `suggested_tools` 写入 user prompt，不只 `estimated_impact`。
- **[P2] [C:⭐ I:⭐⭐] 匹配优先级**：YAML `priority` 或最长 pattern 优先。
- **[P2] [C:⭐ I:⭐⭐⭐] 命中率统计**：`matched_skill` 写入 `node_timings`。
- **[P3] [C:⭐⭐ I:⭐⭐] 热加载 YAML**：监听 `skills/` 变更重载。

---

## 4. Docker 沙箱 — `harness/` / `sandbox_tools.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 镜像预热**：启动前 inspect 镜像，缺失提示 build。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 温容器池**：复用长驻容器 + 增量 tar，缩短每轮 verify。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] tar 增量传输**：只打包变更文件。
- **[P2] [C:⭐ I:⭐⭐] 并行自愈容器隔离**：池化时每 turn FS 快照或换容器。
- **[P1] [C:⭐ I:⭐⭐⭐] 补丁上限强制**：`RepoPatchApplier` 已有常量；Orchestrator 调用侧拒绝超限 patch。
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 容器内打补丁**：`--patch-target container`，宿主机保持 bug 态。
- **[P2] [C:⭐ I:⭐⭐] 验证后导出 diff**：容器修复成功 → `docker cp` → 可选 `git apply`。
- **[P1] [C:⭐ I:⭐⭐⭐] 容器 PatchApplier E2E**：`@pytest.mark.docker` 测 apply + revert。

---

## 5. 验证语义 — `repair/verify.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] verify-scope 开关**：`related|all` 控制 pytest 范围。
- **[P1] [C:⭐ I:⭐⭐⭐] build_log 反馈**：pip 失败时 feedback 附带 build stderr 摘要。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 容器 lint**：`ruff check` 结果进 `VerificationResult.lint_issues`。
- **[P2] [C:⭐ I:⭐⭐⭐] pytest 超时强化**：无 JSON 报告时仍产出明确 failure_logs。
- **[P2] [C:⭐ I:⭐⭐] Verifier LLM 可选路径**：`harness|agent` 切换；生产默认 harness 直连。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 多语言 profile**：Node 等 Sandbox profile 占位。

---

## 6. Agent 行为 — Localizer / Retriever / Patcher

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] JSON 解析失败重试**：0 suspects/patches 时再 ask 一次并附格式约束。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] original_lines 校验**：应用前比对磁盘，不一致则拒绝。
- **[P2] [C:⭐ I:⭐⭐⭐] Localizer 工具顺序**：Prompt 硬约束 stack_parse → ast_parse。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Retriever 规则快路径**：`--fast-retrieve` 零 LLM 填上下文。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Patcher best-of-N**：多候选 patch 逐个 verify。
- **[P2] [C:⭐ I:⭐⭐⭐] Patcher loop 模式**：可配置回 Agent loop + `patch_file`。
- **[P3] [C:⭐⭐ I:⭐⭐] unified diff 原生**：Patcher 输出标准 diff，统一 apply 路径。
- **[P3] [C:⭐⭐ I:⭐⭐] suspect Top-K 截断**：按 confidence 排序后限制进 Patcher 的 token。

---

## 7. Orchestrator 编排 — 并发 / 自愈 / 安全

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 反馈环 enriched**：失败测试 + 上轮改动 + 回滚说明 + build_log。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 分阶段超时**：localize / patcher / verify 独立 timeout。
- **[P2] [C:⭐ I:⭐⭐⭐] CLI max-retries**：`--max-retries` 对齐 `RepairState.max_retries`。
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] Critic Agent**：Patcher 前只读审查 suspect 是否充分。
- **[P2] [C:⭐ I:⭐⭐⭐] Retriever 失败降级**：用 `search`/`rg` 按堆栈文件名补上下文。
- **[P2] [C:⭐ I:⭐⭐⭐] ToolGateway 审计**：越权记 trace / agent_errors。
- **[P2] [C:⭐ I:⭐⭐⭐] 补丁黑名单**：拒绝 patch `.git/`、`*.pyc`、`uv.lock` 等。
- **[P2] [C:⭐⭐ I:⭐⭐] asyncio 流水线**：可选 async docker；当前 ThreadPool 并行已满足 M6 需求。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 动态重试策略**：第 2 轮起扩大检索或放宽文件白名单。

---

## 8. CLI 与可观测性 — `src/cli.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 退出码语义**：按 `state.status` 返回 0/1/2/3。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] repair --json**：stdout JSON，stderr 人类日志。
- **[P2] [C:⭐ I:⭐⭐⭐] --timeout 暴露**：全流程 timeout 可配置。
- **[P2] [C:⭐ I:⭐⭐⭐] dry-run 贯穿 Verifier**：Verifier 跳过 Docker 或 mock。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] repair doctor**：检查 env、Docker、镜像、demo。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Repair 专用 trace**：`.agent/repairs/{id}/trace.jsonl` 阶段摘要（redact 后）。
- **[P2] [C:⭐ I:⭐⭐⭐] 单次 repair 指标卡**：retry、各阶段 ms、token 汇总打印或 JSON 字段。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 打通 Layer 1 RunStore**：repair run_id 关联 `.agent/runs/` 可 `/replay`。

---

## 9. Prompt 工程 — `src/prompts/`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] prompt-ab-results.md**：4 Agent × 变体 × 3 case 的解析率与 Fix 率记录。
- **[P2] [C:⭐ I:⭐⭐⭐] Prompt 版本号**：文件头 version 写入 trace。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 失败驱动片段**：从 agent_errors 自动生成禁止事项段。

---

## 10. 测试与 Demo

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Docker CI job**：可选 workflow 跑 `@pytest.mark.docker`。
- **[P1] [C:⭐ I:⭐⭐⭐] CLI 退出码单测**：failed 时非零 exit。
- **[P2] [C:⭐ I:⭐⭐⭐] Blackboard 集成测**：冲突与仲裁路径。
- **[P2] [C:⭐ I:⭐⭐] Skill 匹配单测**：demo issue → 预期 yaml。
- **[P2] [C:⭐ I:⭐⭐⭐] test_self_healing 独立文件**：从 e2e 拆出便于检索。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] demo case 扩展**：M7 已有 10 Case；demo 目录可继续加 KeyError / 多轮自愈示例。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 两轮自愈 demo 脚本**：专门展示 feedback 环。
- **[P2] [C:⭐ I:⭐⭐⭐] demo case.yaml 元数据**：expected_patch 供自动评分，不进入 Agent prompt。
- **[P3] [C:⭐⭐ I:⭐⭐] asciinema / GIF**：附着 demo 文档。

---

## 11. 文档与叙事

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] design-decisions.md**：M8 已有 10 条 ADR；可增补 Blackboard / patch-target 条目。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] ARCHITECTURE Layer 2**：已有主文档；post-#83 需同步 `src/repair/` 与 tool_policy。
- **[P2] [C:⭐ I:⭐⭐⭐] M5_GUIDE 架构图更新**：Blackboard 标为可选扩展面。
- **[P2] [C:⭐ I:⭐⭐] 验证结果进 trace**：sandbox_timings / failure_logs 写 JSONL 不只 stderr。

---
