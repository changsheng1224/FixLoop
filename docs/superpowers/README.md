# Superpowers × FixLoop Bonus

本仓库集成了 [obra/superpowers](https://github.com/obra/superpowers)（MIT），用于规范 bonus 功能的**设计 → 计划 → TDD 实现 → PR** 流程。

## Agent 设计参照

[2026-10-01 五维对照与技术取舍](../AGENT_DESIGN_REFERENCES_2026-10-01.md)以 OpenCode 为主要参照、Pi 为内核参照、Hermes 为专项补充，并交叉检查 Codex/Claude Code。文档区分已有实现、静态发现和未来保证；当前先确定 [Plan MVP：统一视图与重规划决策](specs/2026-10-01-plan-view-replan-decision-mvp.md)，其他领域逐项讨论。[剩余实施计划](plans/2026-10-01-agent-design-reference-improvements.md)保留候选拆解，旧工期和状态不能代替当前差量。

Plan MVP 两项已实现并通过相关验收，实时阶段、技术取舍和 P1–P7 证据见 [2026-10-01 验收记录](../PLAN_VIEW_REPLAN_ACCEPTANCE_2026-10-01.md)。代码探索已完成 [消费链 MVP 验收](../CODE_EXPLORATION_CONSUMPTION_ACCEPTANCE_2026-10-01.md)。长任务状态与上下文已落实确认的统一投影、必需预算门禁和恢复重建，范围与相关测试见 [MVP 验收](../LONG_TASK_CONTEXT_ACCEPTANCE_2026-10-01.md)；显式决策版本已由下述增量落实，其他领域继续单独讨论。

上下文组装已完成确认的统一请求入口与 Plan 弹性预算，完整工具组、最终请求 manifest、适用路径与技术取舍见 [2026-10-02 验收记录](../CONTEXT_ASSEMBLY_ACCEPTANCE_2026-10-02.md)。输入总上限保持；选定的批次/记录拆分见下文，其他自动化与整体策略迁移继续后置。

证据消费已完成确认的校验解释、当前节点必需事实摘要与最终消费记录，范围及技术取舍见 [2026-10-02 验收记录](../EVIDENCE_CONSUMPTION_ACCEPTANCE_2026-10-02.md)。校验通过、摘要入选与正文入选分别记录；历史依赖不触发已确认写入的重放。

决策与恢复投影已完成确认的显式版本链、当前节点消费与 checkpoint 隔离，完整证据 checksum、进程中断恢复和技术取舍见 [2026-10-02 验收记录](../DECISION_PROJECTION_ACCEPTANCE_2026-10-02.md)。来源复核保留审计价值，自动提炼和全入口迁移继续后置。

内核职责拆分已完成确认的批次编排与 XML/native 共用 Observation 记录，复用既有执行闸口、owner 提交和恢复契约。范围、相关行为验证与技术取舍见 [2026-10-02 验收记录](../KERNEL_SPLIT_ACCEPTANCE_2026-10-02.md)；修复策略整体下沉与完整 L1/L2 解耦继续后置。

工具批次已完成确认的派发前纯参数预检与 native 原始协议核对：单项参数错误保留配对拒绝，合法兄弟继续；身份/结构不一致整批零执行。Executor 权限门禁、串行副作用与既有截断恢复保持，范围、119 项相关通过和技术取舍见 [2026-10-02 验收记录](../BATCH_PREFLIGHT_ACCEPTANCE_2026-10-02.md)。

恢复与取消已完成确认的严格恢复入口和统一诊断结果：checkpoint 无效时零执行，公开状态/进度/报告共享当前投影；取消请求、清理确认与终态分开。失败清理后重复取消不重新操作资源，接管后的旧 generation 仍拒绝。102 项相关通过、实际进程恢复及技术取舍见 [2026-10-02 验收记录](../RECOVERY_CANCEL_ACCEPTANCE_2026-10-02.md)。

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
