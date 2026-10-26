# FixLoop

FixLoop 将通用 Agent 运行时与代码修复业务分成两层。Layer 1 提供模型与工具执行、权限、预算、取消、上下文和事件机制；Layer 2 通过 `RepairService.run(RepairRequest)` 执行工作区准备、探索、Patcher 修改、真实命令验证与结果输出。

当前 API、补丁格式、状态和资源所有权见 [修复契约](docs/REPAIR_CONTRACT.md)。普通 Agent 不需要修复角色或修复配置。

修复阶段由 LangGraph 编排，状态、持久化和执行内核由 FixLoop 管理。参见
[架构与技术讲解](docs/LANGGRAPH_GUIDE.md)、[离线演示](docs/LANGGRAPH_DEMO.md) 和
[定向验收](docs/LANGGRAPH_D5_ACCEPTANCE.md)。

## 安装

需要 Python 3.11 或更新版本。

```sh
python -m pip install -e ".[dev]"
```

配置模型凭据时参考 `.env.example`。始终使用同一个 Python 解释器执行 pip、程序和测试。

## 使用

```sh
python -m agent_runtime "读取 README 并总结"
python -m src.cli repair --repo demo/calculator --workspace-policy provided --execution-tier host --issue "修复除零错误，并运行相关测试"
python -m src.cli eval --case case_001 --fake --output eval_results/smoke
python -m src.cli workbench --open-browser
```

本地修复默认 `--workspace-policy worktree`，要求干净 Git 根目录并创建独立副本。`provided` 直接使用指定目录。`--skip-verify` 只能产生待验证结果，不能标记 `fixed`。Workbench 项目修复使用受治理的 WSL/bubblewrap 后端；普通聊天使用独立聊天流。

离线评测的 `--fake` 使用内置固定模型响应，仍经过正式服务、工具修改和真实测试。答案资源仅用于离线测试与独立判分，不会提供给真实模型。wheel 包含 `.diff` 等评测资源。

## Python API

```python
from src.repair.assembly import create_repair_service
from src.repair.service import RepairRequest

service = create_repair_service(
    client, repo_path, workspace_policy="provided", execution_tier="host"
)
limits = service.session.patcher.config.snapshot()  # 有效配置、来源和 hash
result = service.run(RepairRequest(
    issue="修复错误", verification_target="tests/test_api.py", verification_min_tests=1
))
```

配置与版本见 [状态和配置](docs/REPAIR_STATE_CONFIG_CONTRACTS.md)，通用执行接口见 [运行时契约](docs/RUNTIME_CONTRACTS.md)。旧状态和 checkpoint 明确拒绝，不自动迁移。

## 测试与打包

```sh
python -m pytest tests/test_patch_contract.py tests/test_patch_backends.py tests/test_execution_contract.py -q -p no:cacheprovider
python -m ruff check agent_runtime src tests
python -m ruff format --check agent_runtime src tests
python scripts/wheel_smoke.py
```

PR 自动运行 Linux/Windows 轻量契约测试和干净 wheel 离线冒烟。全量测试由手动 workflow 触发；Docker 与真实模型评测手动执行。本地全量测试遵守 [CLAUDE.md](CLAUDE.md) 的授权约定。

[历史记录](docs/history/README.md) 仅供人工查看，不是当前接口说明。
