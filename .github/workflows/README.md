# GitHub Actions（默认关闭自动触发）

Workflow 文件已就绪，但 **`on` 仅保留 `workflow_dispatch`**，不会在 push/PR 时自动运行。

## 本地运行（与 workflow 步骤一致）

```bash
ruff check agent_runtime src tests
ruff format --check agent_runtime src tests
pytest tests/ -v --cov=agent_runtime --cov=src

python -m src.eval.runner --ci
python -m src.eval.regression_check \
  --current eval_results/ci/eval_report.json \
  --baseline src/eval/ci_baseline_report.json
```

## 启用自动 CI

编辑 `test.yml` / `eval.yml`：

1. 取消 `push` / `pull_request` 注释  
2. 注释或删除 `workflow_dispatch:`  

## 手动在 GitHub 上跑

**Actions** → 选择 **Test** 或 **Eval** → **Run workflow**
