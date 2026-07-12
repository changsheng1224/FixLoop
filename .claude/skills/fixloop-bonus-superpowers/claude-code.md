# Claude Code 平台细则 — FixLoop Bonus

> 通用工作流：[fixloop-bonus-superpowers.md](../../fixloop-bonus-superpowers.md)

## Skill 调用

Claude Code 通过 **Skill 工具** 加载 skill（**无** `superpowers:` 前缀）：

```text
Skill({ skill: "using-superpowers" })
Skill({ skill: "fixloop-bonus-superpowers" })
Skill({ skill: "brainstorming" })
```

Bonus 开发会话建议顺序：`using-superpowers` → `fixloop-bonus-superpowers` → 阶段 skill。

也可 **Read** 文件代替 invoke（内容等价）：

- `.claude/fixloop-bonus-superpowers.md`
- `.claude/skills/fixloop-bonus-superpowers/reference.md`

## Subagent（大项实现）

Claude Code 支持 **Agent** 工具。大 plan 优先：

```text
Skill({ skill: "subagent-driven-development" })
```

- 每个子任务单独 prompt，含 FixLoop Context + 文件路径 + 验收 pytest
- 用 `description` 标注 agent 职责（如「implement blackboard merge」）
- 子 agent 返回最终摘要供主会话 review

并行独立探路时用 `dispatching-parallel-agents`。

## Git Worktree

隔离实验 / 并行方案：

```text
Skill({ skill: "using-git-worktrees" })
```

Claude Code 提供 `EnterWorktree` / `ExitWorktree`；与 superpowers worktree skill 兼容。

检测是否在 worktree：

```bash
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
GIT_COMMON=$(git rev-parse --git-common-dir 2>/dev/null)
# GIT_DIR != GIT_COMMON → linked worktree
```

## 与 Cursor 的差异

| 项 | Cursor | Claude Code |
|----|--------|-------------|
| 自动路由 | `.cursor/rules/superpowers-workflow.mdc` | 需 Invoke skill 或 Read 本目录文档 |
| Skill 路径 | `.cursor/skills/<name>/SKILL.md` | `.claude/skills/<name>/SKILL.md` |
| 子 agent | Task 工具 | Agent 工具 |
| 项目约定 | rules + CLAUDE.md | **CLAUDE.md**（根目录）+ 本 skill |

## 推荐会话开场（用户可复制）

```text
从 docs/bonus.md §12 做 [条目名]。请先 invoke fixloop-bonus-superpowers，
说明方案等我确认后再实现。
```

## 权限与 Bash

`.claude/settings.local.json` 可配置允许命令。FixLoop 开发常需：

- `git checkout`, `git commit`, `git push`
- `pytest tests/...`
- `gh pr create`, `gh pr merge`

若命令被拦，请用户加入 allowlist 或在本机执行 push/PR。
