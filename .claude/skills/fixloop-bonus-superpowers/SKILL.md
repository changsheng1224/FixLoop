---
name: fixloop-bonus-superpowers
description: >-
  FixLoop bonus 与功能扩展工作流（Claude Code）。用于 bonus、P1/P2、Layer1/Layer2、
  docs/bonus.md 条目、agent_runtime 或 src 实现。流程 brainstorming → plan → TDD → PR。
  Claude Code 下先 invoke 本 skill，再读 .claude/fixloop-bonus-superpowers.md。
---

# FixLoop Bonus + Superpowers（Claude Code）

FixLoop 在 Claude Code 中的 **Bonus 开发入口 skill**。

## 立即执行

1. Read 根目录 **`CLAUDE.md`**（硬约束）
2. Read **`.claude/fixloop-bonus-superpowers.md`**（完整工作流）
3. Read **[reference.md](reference.md)**（模块映射、已落地项）
4. 平台细则：**[claude-code.md](claude-code.md)**（Skill / Agent / Worktree）
5. 声明：`Using fixloop-bonus-superpowers to <purpose>`

## 硬约束摘要

- 动手前向用户说明方案并**获确认**
- 单任务：**相关 pytest only**；PR 前：`pytest tests/ -v`
- 分支 → PR（title=分支名，body 中文）→ squash merge；**禁止 push master**
- L1（`agent_runtime/`）**不得** `import src.*`
- gh 代理：`$env:HTTPS_PROXY="http://127.0.0.1:7897"`（Windows）

## 流程速查

```text
选题 → brainstorming → spec → writing-plans → TDD + executing-plans
     → 相关 pytest → commit（用户要求）→ 全量 pytest → finishing-a-development-branch
```

| 下一步 | Invoke skill |
|--------|--------------|
| 设计 | `brainstorming` |
| 计划 | `writing-plans` |
| 实现 | `test-driven-development` + `executing-plans` 或 `subagent-driven-development` |
| 调试 | `systematic-debugging` |
| PR/合并 | `finishing-a-development-branch` |

Spec → `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md`  
Plan → `docs/superpowers/plans/YYYY-MM-DD-<slug>.md`

## FixLoop Context（spec/plan 必含）

```markdown
- **Bonus ref:** docs/bonus.md §N
- **Layer:** L1 | L2 | both
- **Primary modules:** ...
- **Acceptance:** pytest ...
- **Branch:** V1.x-BonusN-<slug>
```

## 附加资源

- 完整文档：[../../fixloop-bonus-superpowers.md](../../fixloop-bonus-superpowers.md)
- Cursor 等价：`.cursor/skills/fixloop-bonus-superpowers.md`
- Superpowers 路由：`../using-superpowers/SKILL.md`
