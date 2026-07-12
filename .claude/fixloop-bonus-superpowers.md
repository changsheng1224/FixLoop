# FixLoop Bonus + Superpowers（Claude Code 版）

FixLoop 在 **Claude Code** 中做 Bonus / 功能扩展的标准工作流。项目硬约束以仓库根目录 **`CLAUDE.md`** 为准；本文件补充 Claude Code 下的 skill 调用与工具习惯。

**配套文件（均在 `.claude/`）**

| 文件 | 用途 |
|------|------|
| [skills/fixloop-bonus-superpowers/SKILL.md](skills/fixloop-bonus-superpowers/SKILL.md) | Skill 工具入口（`Skill({ skill: "fixloop-bonus-superpowers" })`） |
| [skills/fixloop-bonus-superpowers/reference.md](skills/fixloop-bonus-superpowers/reference.md) | 模块映射、已落地清单、验收矩阵 |
| [skills/fixloop-bonus-superpowers/claude-code.md](skills/fixloop-bonus-superpowers/claude-code.md) | Claude Code 平台细则 |
| `docs/bonus.md` | 待办 backlog |
| `docs/bonus/DESIGN.md` | 设计边界 |

Cursor 版等价文档：`.cursor/skills/fixloop-bonus-superpowers.md`

---

## 0. Claude Code 启动协议

Bonus / 功能开发任务**第一条用户消息**起：

1. **Invoke** `using-superpowers`（若尚未在本会话执行）
2. **Invoke** `fixloop-bonus-superpowers`（或 Read 本文件 + `reference.md`）
3. 声明：`Using fixloop-bonus-superpowers to <purpose>`
4. Read 根目录 `CLAUDE.md` 中与测试/Git 相关的段落

**触发条件**（满足任一即适用本工作流）：

- bonus、P1/P2、Layer1/Layer2 扩展
- 引用 `docs/bonus.md` 或 `docs/superpowers/specs|plans/`
- 修改 `agent_runtime/` 或 `src/`（非纯解释）

---

## 1. FixLoop 硬约束

| # | 规则 |
|---|------|
| 1 | **动手前说明并获确认**：做什么 + 怎么实现（模块/决策/预估行数） |
| 2 | **单任务只跑相关 pytest**；**提 PR 前** `pytest tests/ -v` |
| 3 | 独立分支 → `gh pr create` → squash merge；**禁止直接 push master** |
| 4 | **最小 diff**；大范围重构单独 commit/PR |
| 5 | **L1 不 import L2**：`agent_runtime/` 禁止 `import src.*` |
| 6 | gh 需代理（Windows PowerShell）：见 §5 |

`CLAUDE.md` 与用户明确指令优先于本 skill。

---

## 2. 端到端流程

```text
选题 → DESIGN 章 → Skill: brainstorming → spec → Skill: writing-plans → plan
     → Skill: test-driven-development + executing-plans（或 subagent-driven-development）
     → 相关 pytest → commit（用户要求时）→ PR 前全量 pytest → Skill: finishing-a-development-branch
```

### 阶段 → Skill 映射（Claude Code）

| 阶段 | Invoke | 产出 |
|------|--------|------|
| 路由 | `fixloop-bonus-superpowers` | 选题清单 |
| 设计 | `brainstorming` | `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md` |
| 计划 | `writing-plans` | `docs/superpowers/plans/YYYY-MM-DD-<slug>.md` |
| 实现 | `test-driven-development` + `executing-plans` 或 `subagent-driven-development` | 代码 + 测试 |
| 调试 | `systematic-debugging` | 根因修复 |
| 验证 | `verification-before-completion` | 测试证据 |
| 收尾 | `finishing-a-development-branch` | PR / merge |

大项实现优先 **`subagent-driven-development`**（Claude Code 支持 `Agent` 工具派生子 agent）。

---

## 3. 选题检查清单

- [ ] `docs/bonus.md` 条目 **P/C/I**；背景 `docs/bonus/DESIGN.md`
- [ ] **Layer**：L1 | L2 | both
- [ ] 是否已有 ✅（增强 vs 从零）— 见 `reference.md` 已落地表
- [ ] **验收**：相关 pytest / eval case / demo repair
- [ ] **分支名**（§5）
- [ ] 规模：小 &lt;100 · 中 100–400 · 大 &gt;400 行

---

## 4. Spec / Plan 模板

```markdown
## FixLoop Context
- **Bonus ref:** docs/bonus.md §N — [标题]
- **Layer:** L1 | L2 | both
- **Primary modules:** ...
- **Acceptance:** pytest tests/test_....py -v / eval / demo
- **Branch:** V1.x-BonusN-<slug>
```

Plan 中每个任务注明：文件路径、TDD 顺序、相关 pytest 命令（**非全量**）。

---

## 5. Git / 分支 / PR

### 分支命名

```text
V{major}.{minor}-Bonus{n}-{slug}
M{m}/D{d}/{task-slug}
bonus/<short-slug>
```

### 工作流（Bash / PowerShell）

```bash
git checkout master && git pull
git checkout -b V1.2-Bonus7-<slug>
# 开发 + 相关 pytest
git add ... && git commit -m "feat(V1.2-Bonus7): <描述>"
git push -u origin HEAD
```

**Windows — gh 代理：**

```powershell
$env:HTTPS_PROXY="http://127.0.0.1:7897"
$env:HTTP_PROXY="http://127.0.0.1:7897"
$env:PATH="$env:PATH;C:\Program Files\GitHub CLI"
gh pr create --base master --title "V1.2-Bonus7-<slug>" --body "..."
pytest tests/ -v
gh pr merge --squash --delete-branch
git checkout master && git pull
```

- **PR title = 分支名**
- **PR body 中文**：`## 摘要` · `## 测试计划`
- 仅在用户明确要求时 `git commit` / `git push`

---

## 6. 测试策略

| 时机 | 命令 |
|------|------|
| 单任务实现 | `pytest tests/test_<相关>.py -v` |
| 提 PR 前 | `pytest tests/ -v` |

| 改动域 | 相关测试 |
|--------|----------|
| `agent_runtime/*` | `tests/test_agent_loop.py`, `test_context_manager.py` |
| `src/repair/*` | `tests/test_orchestrator.py`, `test_repair_*.py`, `test_blackboard_*.py` |
| degrade | `tests/test_repair_degrade.py` |
| L2 binding | `tests/test_l2_binding.py`, `test_l2_state_binding.py` |

---

## 7. Layer 与模块速查

- **L1** `agent_runtime/` — Loop、Context、Tools
- **L2** `src/` — Orchestrator、`src/repair/`、eval
- **禁止** L1 → L2 import；L1 trace 用 `agent_runtime/l2_context.py`

详见 [reference.md](skills/fixloop-bonus-superpowers/reference.md)。

---

## 8. L2 模式（Bonus6 已落地）

1. Blackboard write-only → patch 边界 merge
2. Prefix 订阅 prompt 块
3. `agent_asks` + L2 binding
4. `RepairRunContext` 会话状态
5. Degrade 加固 + `--no-degrade`

新功能优先 mixin 拆分，避免 `pipeline.py` 膨胀。

---

## 9. 意图 → Skill

| 用户意图 | 首先 Invoke |
|----------|-------------|
| 做 bonus §X | `fixloop-bonus-superpowers` → `brainstorming` |
| 按推荐方案实现 | `test-driven-development` + `executing-plans` |
| 规划拆任务 | `writing-plans` |
| 修失败测试 | `systematic-debugging` |
| commit / PR / 合并 | `finishing-a-development-branch` |
| 并行探方案 | `dispatching-parallel-agents` |
| 隔离实验分支 | `using-git-worktrees` |

---

## 10. 反模式

- ❌ 跳过 skill / 未确认就改代码
- ❌ 单任务跑全量 pytest
- ❌ 提 PR 不跑全量
- ❌ L1 import src
- ❌ 直接 push master
- ❌ 重复实现 reference 中 ✅ 项

---

## 11. 单任务自检

```text
[ ] Invoke fixloop-bonus-superpowers + Read reference
[ ] 说明方案，获用户确认
[ ] brainstorming / writing-plans（如需）
[ ] TDD + 相关 pytest
[ ] 用户要求时再 commit
[ ] 分支完成：pytest tests/ -v → PR → merge
```

---

## 12. 维护 Superpowers

```powershell
powershell -File scripts/update-superpowers-skills.ps1
```

同步更新 `.claude/skills/` 与 `.cursor/skills/` vendored 副本。
