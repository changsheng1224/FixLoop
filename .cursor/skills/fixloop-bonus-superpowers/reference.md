# FixLoop Bonus Reference（for Superpowers）

## 权威文档

| 文档 | 用途 |
|------|------|
| `docs/bonus.md` | **待实现 backlog**（P/C/I 条目，不含 ✅ 已完成项） |
| `docs/bonus/DESIGN.md` | 设计边界、架构、现状、Gap、面试要点 |
| `docs/bonus/OUT_OF_SCOPE.md` | Web 产品化归档（主线不实现） |
| `docs/bonus/README.md` | 上述文档索引 |
| `CLAUDE.md` | 执行确认、测试、Git/PR、代理 |
| `LAYER1_GUIDE.md` | Agent 运行时导读 |
| `LAYER2_GUIDE.md` | 多 Agent 修复导读 |
| `docs/DEVELOPMENT_PLAN_ALL.md` | 原始 M1–M8 设计 |

## Layer 边界

- **Layer 1** (`agent_runtime/`)：单 Agent 生命周期、Loop、Context、Tools、Memory、Provider
- **Layer 2** (`src/`)：Localizer / Retriever / Patcher / Verifier、Orchestrator、eval cases

L2 通过 `repair_factory` 创建多个 L1 Agent 实例；bonus 改 L2 时通常同时 touch `tests/test_orchestrator.py`。

## bonus.md 章节 → 目录速查

| § | 域 | 主要代码 |
|---|-----|----------|
| 1–2 | 运行时 / Loop | `runtime.py`, `agent_loop.py`, `config.py` |
| 3 | Context | `context_manager.py`, `tokenizers.py` |
| 4 | Memory | `agent_runtime/features/memory/` |
| 5–8 | Prompt / Tool / Gateway / 安全 | `prompt_prefix.py`, `tools.py`, `tool_executor.py` |
| 9–11 | 配额 / 熔断 / Checkpoint | `quota`, `circuit_breaker.py`, `checkpoint.py` |
| 12 | Multi-Agent | `src/orchestrator.py`, `src/repair/` |
| 13 | Skill | `src/skills/*.yaml` |
| 14–17 | 输出 / 自愈 / 沙箱 / Patch | `output_parsers.py`, `harness/`, `patch_applier.py` |
| 18–19 | 安全 / 可观测 | `security.py`, `run_trace.py`, `run_store.py` |
| 20 | 评测 | `src/eval/` |
| 21–22, 24–25 | CLI / 配置 / 压测 / Demo | `cli.py`, `demo/` |
| **23** | **意图识别 / 路由** | `orchestrator._parse_issue`, `_match_skill`, `_has_save_intent` |

## 近期已落地（master @ PR #87 附近）

- 统一 repair trace（`src/repair/run_trace.py`）
- Per-agent token / tool 统计
- `prompt_budget` + multi-tokenizer（`agent_runtime/tokenizers.py`）
- **L0–L5 压缩管线**（`compression_pipeline.py` · PR #87）
- **本地运行产品边界**（bonus 文档拆分 · Web → OUT_OF_SCOPE）

新 bonus 条目应检查是否与上述重叠，避免重复开发。

## 分支命名

优先：

```text
bonus/<short-slug>     # 例如 bonus/cancellation-token
M{m}/D{d}/<task-slug>  # 与主线 sprint 对齐时
```

## 验收矩阵

| 改动类型 | 最低验收 | PR 前 |
|----------|----------|-------|
| L1 runtime | `tests/test_agent_loop.py`, `test_context_manager.py` 等 | `pytest tests/ -v` |
| L2 repair | `tests/test_orchestrator.py` + 相关 agent tests | 同上 |
| 可观测 | 读 `.agent/runs/.../trace.jsonl`, `report.json` | demo `--verbose` |
| Eval 相关 | `python -m src.cli eval --case case_XXX` | eval + 全量 pytest |

## Spec / Plan 路径

- 设计：`docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md`
- 计划：`docs/superpowers/plans/YYYY-MM-DD-<slug>.md`

## 不推荐重复做的条目

`docs/bonus.md` §1「统一 token 会计」——核心 repair 可观测已在 PR #86 覆盖；若要做，限定为 session 级 cache_read 等 API 细项，而非重写 trace。
