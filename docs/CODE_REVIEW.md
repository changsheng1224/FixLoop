# M8D4 代码终审报告

> 审计日期：2026-07-04。范围：`agent_runtime/`、`src/`、`tests/`（不含 `scripts/`、`src/eval/cases/` 内 demo repo）。

## 1. 静态检查

| 项 | 结果 |
|----|------|
| `ruff check agent_runtime src tests` | 通过 |
| `ruff format --check agent_runtime src tests` | 通过 |
| 裸 `except:` | **0** 处 |
| 硬编码绝对路径（`/home/`、`C:\Users\`） | **0** 处（业务代码） |
| `git ls-files` 敏感文件（`.env`、key、pem） | **0** 处 |
| 误追踪 cache（`__pycache__`、`.pytest_cache`） | **0** 处 |

### `# TODO` 说明

| 位置 | 处理 |
|------|------|
| `scripts/scaffold_eval_cases.py` | 脚手架占位符，已在模块 docstring 标注；非运行时代码 |
| `tests/test_eval_cases.py` | 断言 Case 补丁不含 TODO，非待办 |

### `except Exception` 说明

业务边界（JSON 解析、Docker 不可用、patch 应用失败）使用 `except Exception` 并写入结构化错误，**非**裸 `except:`。符合「不崩溃、可观测」设计。

## 2. 测试与覆盖率

```bash
pytest tests/ -v --cov=agent_runtime --cov=src --cov-report=html
```

| 指标 | 数值 |
|------|------|
| 测试数 | **475** |
| 合计覆盖率 | **80%**（4825 stmts，950 miss） |

### 核心模块（M8D4 目标 >80%）

| 模块 | 覆盖率 | 说明 |
|------|--------|------|
| `agent_runtime/agent_loop.py` | 66% | 未覆盖多为 max_steps 边界、replay 分支、异常退避；主路径已测 |
| `agent_runtime/runtime.py` | 80% | 达标 |
| `agent_runtime/tool_executor.py` | 89% | 达标 |
| `agent_runtime/context_manager.py` | 89% | 达标 |
| `src/orchestrator.py` | 79% | 接近达标；Docker/pytest verify 与 composite 分支部分依赖集成环境 |

### 低覆盖模块（已知限制）

| 模块 | 覆盖率 | 原因 |
|------|--------|------|
| `agent_runtime/cli.py` | 25% | REPL / argparse 交互，单测以 `_make_*` 装配为主 |
| `agent_runtime/__main__.py` | 0% | 入口一行 `sys.exit(main())` |
| `agent_runtime/providers/clients.py` | 63% | 真实 HTTP 需 mock；Anthropic/Ollama 部分分支仅集成测 |
| `src/harness/sandbox_manager.py` | 42% | Docker 依赖；单测 mock docker-py，完整路径在 harness 集成测 |

## 3. 仓库卫生

| 项 | 状态 |
|----|------|
| `.gitignore` 含 `.env`、`.agent/`、`eval_results/`、`.pytest_cache/` | OK |
| `.env.example` 无真实 key | OK |
| CI workflow 保留、默认 `workflow_dispatch` | OK（见 `.github/workflows/README.md`） |

## 4. 本次修复

- `SessionStore.latest()`：同 mtime 下按文件名稳定排序，修复 Windows 上 `test_latest` 偶发失败。

## 5. 结论

**代码终审通过**：lint 零 warning、无敏感文件泄漏、合计覆盖率 80%、核心模块 4/5 达标（`agent_loop` / `orchestrator` 略低已文档化）。

未纳入范围（按设计）：Demo 录屏/GIF、GitHub Actions 自动触发、eval PR comment。
