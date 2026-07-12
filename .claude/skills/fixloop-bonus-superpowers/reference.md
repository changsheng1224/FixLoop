# FixLoop Bonus Reference（Claude Code）

> 工作流主文档：[fixloop-bonus-superpowers.md](../../fixloop-bonus-superpowers.md)

## 权威文档

| 文档 | 用途 |
|------|------|
| `docs/bonus.md` | **待实现 backlog**（P/C/I，含 ✅/🔶） |
| `docs/bonus/DESIGN.md` | 设计边界、Gap |
| `docs/bonus/OUT_OF_SCOPE.md` | Web 归档 |
| `CLAUDE.md` | 执行确认、测试、Git/PR、代理 |
| `LAYER1_GUIDE.md` / `LAYER2_GUIDE.md` | Layer 导读 |

## Layer 边界

- **L1** `agent_runtime/`：单 Agent 运行时
- **L2** `src/`：Multi-Agent 修复、eval
- L1 禁止 `import src.*`；L1 trace 用 `agent_runtime/l2_context.py`

## bonus.md § → 代码

| § | 域 | 主要路径 |
|---|-----|----------|
| 1–2 | 运行时 / Loop | `agent_runtime/agent_loop.py` |
| 3 | Context | `context_manager.py`, `compression_pipeline.py` |
| 12 | Multi-Agent | `src/orchestrator.py`, `src/repair/` |
| 13 | Skill | `src/skills/*.yaml` |
| 20 | 评测 | `src/eval/` |

## 已落地（勿重复）

**PR #106 `V1.2-Bonus6-Multi-Agent`**

- 分阶段超时、`RepairPhaseClock`
- L1/L2 绑定、`AgentAskRef`、`agent_asks`、`l2_context.py`
- Blackboard write / patch merge / prefix subscribe
- Degrade 加固、`--no-degrade`
- `l2_ask_mixin`, `blackboard_mixin`, `RepairRunContext`

**更早：** 统一 repair trace、CancellationToken、prompt_budget、L0–L5 压缩（PR #86–#93）

## 分支命名

```text
V{major}.{minor}-Bonus{n}-{slug}
M{m}/D{d}/{task-slug}
```

## 验收矩阵

| 改动 | 单任务 | PR 前 |
|------|--------|-------|
| L1 | `tests/test_agent_loop.py` 等 | `pytest tests/ -v` |
| L2 repair | `tests/test_orchestrator.py` + 域内 | 同上 |
| Blackboard | `tests/test_blackboard_*.py` | 同上 |

## Spec / Plan 路径

- `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md`
- `docs/superpowers/plans/YYYY-MM-DD-<slug>.md`

## 勿重复做

- §1 统一 token 会计（repair 侧已覆盖）
- §12.3 Blackboard 主路径、§10.2 降级（PR #106）
