# Code Exploration MVP 验收证据

日期：2026-09-30。评测输入为 `tests/fixtures/code_exploration/tasks.json` 的 8 个固定任务，oracle 单独存放于 `oracles.json`，仅用于离线评分。三模式运行均从同一 fixture 新建独立 Git 仓库；模型运行每次另建 Agent session。仓库 HEAD、dirty 状态、源码/fixture hash、实际工具 schema、模型与预算见各运行的 `run_manifest.json`。

| 条目 | 可检查证据 |
|---|---|
| A1 错误到定义 | `error_definition` 离线逐次结果；真实 Agent 逐次 trace |
| A2 别名/同名 | `same_name`、`alias_reference`；`tests/test_code_lsp_integration.py` 真实 pylsp 两例 |
| A3 测试/相对导入 | `test_relation` 关系边；`tests/test_code_relations.py::test_relative_import_links_only_to_observed_target` |
| A4 动态/未解析 | `dynamic_unknown`；关系视图不生成运行时调用边；`repair_import` 修复前导入目标保持未解析 |
| A5 修改/删除/重命名 | `tests/test_code_relations.py` 的源码变化、成功写入及删除用例；旧视图整体清空 |
| A6 新增引用者 | `tests/test_code_relations.py::test_new_referrer_requires_and_appears_in_fresh_query` |
| A7 LSP 失败与降级 | `tests/test_code_lsp.py::test_unavailable_server_returns_candidates`；真实 pylsp 集成结果 |
| A8 大文件与命中限制 | `tests/test_code_retrieval_limits.py` |
| A9 Unicode/URI/敏感路径 | `tests/test_code_lsp.py` 的 UTF-16、URI 与外部位置用例；`tests/test_code_retrieval_limits.py` |
| A10 checkpoint 恢复 | `tests/test_code_exploration_resume.py`、`tests/test_checkpoint_resume.py`、`tests/test_strong_step_resume.py` |
| A11 真实修复 | `agent-final-2026-09-30` 中 `repair_import` 的逐次 patch、Observation ID、`test_app.py` 退出码 |

离线运行：`eval_results/code_exploration_mvp/offline-final-2026-09-30/`。真实模型运行：`eval_results/code_exploration_mvp/agent-final-2026-09-30/`。完整逐次 trace 留在本机被 `.gitignore` 排除；本目录提交 `offline-report.md`、`agent-report.md` 和两组逐次结果 JSONL 供 PR 复核。两种结果分开，离线探针不用于宣称 Agent 效果。

真实 LSP 集成命令：

```powershell
python -m pytest -q tests/test_code_lsp_integration.py
```

该集成测试实际启动 `pylsp`，检查定义、引用、同名符号和子进程退出。针对性测试与 Ruff 结果以最终运行记录为准。未获全量测试授权，本阶段不运行完整 `pytest tests/`。

最终相关回归：`143 passed, 1 skipped`（57.07 秒）；真实 LSP 集成的两例均通过。Ruff 对本阶段评测、关系与测试文件检查通过，`git diff --check` 通过。Pytest 报出的缓存目录创建警告不影响测试结果。

离线对照共 24 行，24 行执行成功；8 个任务的定义位置探针均命中，关系组的 3 条可观察关系通过 oracle 检查，故意损坏的导入在修复前仍未解析。真实模型共 72 行，无执行错误；9 个修复任务均有补丁且 `test_app.py` 退出码为 0。其余 63 行的自动路径提及字段只是初筛，不能当作完整语义正确率。
