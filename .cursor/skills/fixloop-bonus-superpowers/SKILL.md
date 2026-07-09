---
name: fixloop-bonus-superpowers
description: >-
  FixLoop bonus 与功能扩展的 Superpowers 入口。自动用于 bonus、P1/P2 增强、
  Layer1/Layer2 新功能、docs/bonus.md 条目、agent_runtime 或 src 实现任务。
  流程：brainstorming → writing-plans → TDD → PR。无需用户 @ 提及。
---

# FixLoop Bonus + Superpowers

FixLoop bonus 开发的**项目入口 skill**。先读 [reference.md](reference.md)，再按 Superpowers 流程推进。

## 0. 自动启用

由 `.cursor/rules/superpowers-workflow.mdc`（`alwaysApply: true`）注入：bonus/功能开发会话**无需** `@fixloop-bonus-superpowers`，Agent 会先 Read 本 skill。

可选 Cursor 官方插件（与 vendored 二选一）：`/plugin-add superpowers`

## 1. FixLoop 硬约束（高于默认 Superpowers 习惯）

来自 `CLAUDE.md` / `.cursor/rules/fixloop-project-conventions.mdc`：

1. **动手前说明并确认**：向用户说明「做什么 + 怎么实现（模块/决策/预估行数）」，**得到确认后再写代码**（与 brainstorming 的 design approval 叠加，不冲突）。
2. **测试**：改完跑相关测试；推 PR 前 `pytest tests/ -v`。
3. **Git**：独立分支 → PR → squash merge；**禁止直接 push master**。
4. **gh 代理**（Windows/PowerShell）：
   ```powershell
   $env:HTTPS_PROXY="http://127.0.0.1:7897"; $env:HTTP_PROXY="http://127.0.0.1:7897"
   ```
5. **最小 diff**：只改 bonus 条目所需范围，不顺手重构。

## 2. Bonus 开发标准流程

```text
读 docs/bonus/DESIGN.md 对应章 → 从 docs/bonus.md 选 P 级待办 → brainstorming → spec → writing-plans → 执行 → 验证 → finishing-a-development-branch
```

| 阶段 | 使用 skill | FixLoop 定制 |
|------|------------|--------------|
| 选题 | 本 skill + `reference.md` | 设计见 `docs/bonus/DESIGN.md`；待办从 `docs/bonus.md` 选 § 与 P 级 |
| 设计 | `brainstorming` | spec 存 `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md` |
| 计划 | `writing-plans` | plan 存 `docs/superpowers/plans/YYYY-MM-DD-<slug>.md`；任务含精确路径（`agent_runtime/` vs `src/`） |
| 实现 | `test-driven-development` + `executing-plans` 或 `subagent-driven-development` | Layer1 改 `agent_runtime/`；Layer2 改 `src/` + `tests/` |
| 调试 | `systematic-debugging` | 复现：`demo/*` 或 `src/eval/cases/case_*` |
| 收尾 | `finishing-a-development-branch` | 分支名 `bonus/<slug>` 或 `M{m}/D{d}/<slug>`；PR 前全量 pytest |

**Announce**：每个阶段开头声明 `Using <skill> to <purpose>`。

## 3. 选题检查清单

开始任一 bonus 条目前，在对话中确认：

- [ ] `docs/bonus.md` 待办条目与 **P/C/I** 标注；设计背景见 `docs/bonus/DESIGN.md`
- [ ] 影响 **Layer1** 还是 **Layer2**（见 reference 模块表）
- [ ] 是否已有 ✅ 基础（增强 vs 从零）
- [ ] 验收方式：单测 / eval case / `demo/calculator` repair
- [ ] 预估 diff 规模（小：<100 行，中：100–400，大：>400 → 考虑拆 plan）

## 4. 计划文档必含（FixLoop 扩展）

在 Superpowers plan header 之外，每个 plan 增加：

```markdown
## FixLoop Context
- **Bonus ref:** docs/bonus.md §N — [条目标题]
- **Layer:** L1 | L2 | both
- **Primary modules:** ...
- **Acceptance:** pytest ... / eval case_00X / demo ...
- **Branch:** bonus/<slug>
```

## 5. 执行时模块速查

| 能力域 | 优先阅读 |
|--------|----------|
| Agent 运行时 / Loop | `agent_runtime/runtime.py`, `agent_loop.py`, `LAYER1_GUIDE.md` |
| Context / Token | `agent_runtime/context_manager.py`, `tokenizers.py` |
| Multi-Agent 修复 | `src/orchestrator.py`, `src/repair/`, `LAYER2_GUIDE.md` |
| 评测 | `src/eval/`, `tests/test_orchestrator.py` |
| 可观测 | `src/repair/run_trace.py`, `agent_runtime/run_store.py` |

## 6. 常见任务 → skill 路由

| 用户意图 | 第一个 skill |
|----------|--------------|
| 「做 bonus §X 的 Y」 | 本 skill → `brainstorming` |
| 「帮我规划/拆任务」 | `writing-plans` |
| 「按 plan 开干」 | `executing-plans` 或 `subagent-driven-development` |
| 「修 repair/demo 失败」 | `systematic-debugging` |
| 「推 PR / 合并」 | `finishing-a-development-branch` |
| 「并行探多个方案」 | `dispatching-parallel-agents` |

## 7. 更新 vendored Superpowers

```powershell
powershell -File scripts/update-superpowers-skills.ps1
```

## 附加资源

- FixLoop bonus 索引与模块映射：[reference.md](reference.md)
- 安装与版本说明：`docs/superpowers/README.md`
- Superpowers 上游：https://github.com/obra/superpowers
