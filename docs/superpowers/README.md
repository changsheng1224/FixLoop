# Superpowers × FixLoop Bonus

本仓库集成了 [obra/superpowers](https://github.com/obra/superpowers)（MIT），用于规范 bonus 功能的**设计 → 计划 → TDD 实现 → PR** 流程。

## 已安装内容

| 位置 | 说明 |
|------|------|
| `.cursor/skills/*/` | 14 个上游 core skills（brainstorming、writing-plans、TDD 等） |
| `.cursor/skills/fixloop-bonus-superpowers/` | FixLoop 专用入口（bonus backlog + 项目约束） |
| `docs/superpowers/specs/` | 设计 spec 输出目录 |
| `docs/superpowers/plans/` | 实现 plan 输出目录 |

## 如何使用

**默认已自动启用**：`.cursor/rules/superpowers-workflow.mdc` 会在每个 Agent 会话注入 Superpowers 路由，bonus/功能开发**无需** `@fixloop-bonus-superpowers`。

新开 Agent 会话（`Ctrl+L`）后直接说任务，例如：

- 「从 bonus.md §2 做 CancellationToken」
- 「帮我 brainstorm Agent 池化」

Agent 应自动 Read `using-superpowers` → `fixloop-bonus-superpowers` → `brainstorming` 等 skill。

若未生效：**新开一个 Agent 会话**（规则在会话开始时加载），或 Settings → Rules 确认 `superpowers-workflow` 已启用。

显式附加仍可用：`@fixloop-bonus-superpowers` / `@brainstorming`

## 可选：Cursor 官方插件

若希望使用 marketplace 版（含 hooks 自动激活）：

```text
/plugin-add superpowers
```

与 vendored skills **功能重叠**，一般保留其一即可。更新 vendored 副本见下。

## 更新 vendored skills

```powershell
powershell -File scripts/update-superpowers-skills.ps1
```

```bash
bash scripts/update-superpowers-skills.sh
```

## 许可证

Superpowers skills 版权归 Jesse Vincent / obra，MIT License。见 `.cursor/skills/ATTRIBUTION.md`。
