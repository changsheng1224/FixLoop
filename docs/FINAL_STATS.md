# FixLoop 最终数据统计（M8D5）

> 统计日期：2026-07-04。命令与口径见下文「复现命令」。

## 1. 代码规模

| 范围 | 文件数 | 行数 | 说明 |
|------|--------|------|------|
| Layer 1 `agent_runtime/` | 31 | **4,578** | Agent 运行时内核 |
| Layer 2 `src/`（不含 `eval/cases/` demo repo） | 37 | **5,086** | 多 Agent 修复 + 评测 + Harness |
| **生产代码合计** | **68** | **9,664** | 较 M8 计划 ~3,800 行超出（含 M7 评测、文档化模块与 docstring） |
| `tests/` | 57 | 5,617 | pytest 用例代码 |
| `scripts/` | 2 | 398 | 脚手架等 |
| `demo/`（不含 Case repo） | 8 | 95 | 演示脚本与 calculator |
| **Python 总计** | **125** | **15,281** | 含测试 |

Git 追踪 `.py` 文件：**164** 个（含 `src/eval/cases/` 内 Case demo 源码）。

## 2. 测试

| 指标 | 数值 |
|------|------|
| pytest 用例数 | **476** |
| M8D5 全量验收（2026-07-04） | **476 passed** |
| 历史偶发失败 | `test_pytest_verify_retries_on_failure`（已通过 snapshot 回滚清 `__pycache__` 修复） |
| Ruff lint | **0 warning**（`agent_runtime src tests`） |

## 3. 覆盖率（M8D4 终审，2026-07-04）

```bash
pytest tests/ -v --cov=agent_runtime --cov=src --cov-report=term
```

| 指标 | 数值 |
|------|------|
| 合计覆盖率 | **80%**（4825 stmts，950 miss） |

### 核心模块

| 模块 | 覆盖率 |
|------|--------|
| `agent_runtime/runtime.py` | 80% |
| `agent_runtime/tool_executor.py` | 89% |
| `agent_runtime/context_manager.py` | 89% |
| `src/orchestrator.py` | 79% |
| `agent_runtime/agent_loop.py` | 66% |

详见 [`docs/CODE_REVIEW.md`](CODE_REVIEW.md)。

## 4. 评测（M7 正式消融）

来源：[`README.md`](../README.md) 评测结果表（`eval_results/final_report.md` 本地，gitignore）。

| 项 | 数值 |
|----|------|
| 评测 Case 数 | **10** |
| 正式实验 | `full` + `single` × 10 Case × 3 重复 = **60 runs** |
| **full**（4-Agent）Fix Rate | **30/30（100%）** |
| **single**（Baseline）Fix Rate | **29/30（96.7%）** |
| 合计 Fix Rate | 59/60（98.3%） |
| 平均耗时 | full 31.8s / single 19.7s |
| 平均 Token | full 5182 / single 2581 |
| Patch 精度 | full **1.22** / single **0.94** |
| 引入回归率 | **0%** |

Case 类型：TypeError、ImportError、AttributeError、logic_error、config_error、composite。

## 5. 文档与交付物

| 项 | 数量 / 状态 |
|----|-------------|
| `ARCHITECTURE.md` | ✅ |
| ADR（`docs/design-decisions.md`） | **10** 条 |
| Demo 脚本 | **3**（`demo_1_repair` / `demo_2_self_healing` / `demo_3_ablation`） |
| CI workflow | **2**（`test.yml` / `eval.yml`，默认 `workflow_dispatch`） |
| 代码终审报告 | [`docs/CODE_REVIEW.md`](CODE_REVIEW.md) |
| 简历 Bullet | [`docs/RESUME_BULLETS.md`](RESUME_BULLETS.md) |

## 6. M1–M8 里程碑对照（摘要）

| 里程碑 | 计划测试数 | 实际 | 计划代码量 | 实际（生产） |
|--------|------------|------|------------|--------------|
| M1 | 20 | ✅ 远超 | ~500 | — |
| M4 | 70 | ✅ | ~1,900 | Layer 1 ~4,578 |
| M6 | 95 | ✅ | ~3,000 | — |
| M7 | 100+ | ✅ **475** | ~3,400 | — |
| M8 | 100+ | ✅ | ~3,800 | **9,664** |

## 7. 复现命令

```bash
# 行数（生产）
python -c "from pathlib import Path
def n(r,e=()): 
  t=f=0
  for p in Path(r).rglob('*.py'):
    if '__pycache__' in p.parts or any(x in p.parts for x in e): continue
    t+=len(p.read_text(encoding='utf-8').splitlines()); f+=1
  return f,t
print('agent_runtime', n('agent_runtime'))
print('src', n('src', ('cases',)))
"

# 测试
pytest tests/ -v --tb=no

# 覆盖率
pytest tests/ -q --cov=agent_runtime --cov=src --cov-report=term

# Lint
ruff check agent_runtime src tests
```
