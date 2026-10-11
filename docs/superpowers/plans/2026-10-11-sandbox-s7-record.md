# S7：主流程接入与验收

S7 将已完成的 S6 生命周期与资源保护接入 repair 主流程。Agent/LLM 和可信控制面继续运行在宿主；实际工作区工具、命令、quick test 和最终 pytest 通过任务绑定的 WSL2/bwrap backend 执行。

## 主流程契约

- `wire_orchestrator(..., execution_backend="wsl_bwrap")` 先准备并校验固定发行版，再构造 sandbox `ToolContext`、策略摘要和 `SandboxTaskSpec`。
- `Orchestrator.repair()` 开始时才解析最终 repository/worktree，创建或恢复 `SandboxTaskBinding`，随后以同一 binding 创建 backend，并把同一 `ToolContext` 注入 Patcher。
- backend 的 `task_binding`、Agent context 的 binding、工作区映射和 state root 必须完全一致；不会在 sandbox 初始化失败时回退到宿主工具。
- WSL sandbox 路径禁用 Docker verifier、宿主 pytest、静态 verifier 和 LSP；最终验证使用 `BwrapVerifyStrategy`。
- 正常结束先关闭 backend，再释放协调 owner；未分类异常也走同样的关闭顺序。native cleanup 不确定时状态进入 `recovery_required`，owner/worktree 保留供恢复。

## 使用与复验

已接通显式 WSL2/bwrap repair 入口；CLI 默认 backend 仍为 `legacy`。本地仓库可使用：

```powershell
python -m src.cli repair --repo <仓库绝对路径> --issue "<问题描述>" --execution-backend wsl_bwrap --output <新的结果目录>
```

设置 `FIXLOOP_USE_WORKTREE=1` 可创建任务 worktree；不设置时绑定用户工作区。该 backend 不依赖 Docker；不支持同时指定 legacy execution tier、`--require-sandbox` 或 LSP。首次环境准备遵循 S2/S6 的固定发行包与健康检查，失败关闭。

离线模型实机验收使用独立 fixture 和 state root，命令如下（仅模型响应脚本化，工具执行均真实）：

```powershell
python scripts/wsl_sandbox_s7_probe.py --workspace <FixLoop绝对路径> --state-root <新的state-root> --report <报告绝对路径>
```

## 本轮实现

- `Orchestrator._bind_sandbox_task()` 在 Agent 绑定失败时关闭已 pin 的 backend，避免启动异常遗留环境 pin。
- `Orchestrator.repair()` 在 checkpoint 校验、初始快照、协作上下文或执行前取消等 pipeline 前置阶段失败/返回时，也会关闭已绑定 backend，并保留任务 worktree 的恢复记录。
- `Orchestrator._close_sandbox_backend_before_binding_close()` 覆盖计划初始化或未分类异常，确保 backend admission 先关闭；关闭失败会持久化 `recovery_required`，再关闭 plan binding。
- `repair_factory` 在 `wsl_bwrap` 模式不再创建宿主/Docker Verifier；验证统一由任务绑定的 `BwrapVerifyStrategy` 执行。
- `RepairCollaborationRuntime` 为长时 context/patch/verify 阶段维护 task lease heartbeat，并在 repair 终止时幂等停止，避免默认 60 秒租约在模型调用期间过期。
- heartbeat 与阶段完成串行，只续租本 runtime 的阶段任务；恢复前不启动续租，协调 owner 失效后停止续租；异常 pipeline 退出也关闭 runtime。
- 默认 grounded Plan 的 edit 节点允许 Patcher 环内 `quick_test`，仍按 verify 效果记录并经过工具权限策略；快检不生成最终 `tests_passed` 完成证据，独立 verify 节点仍须执行。
- CLI verbose 在 WSL2/bwrap 模式准确说明任务沙箱验证，避免误报 Docker 不可用、跳过验证。
- `scripts/wsl_sandbox_s7_probe.py` 使用确定性模型响应与真实 CLI 生命周期、工作区 backend、Agent quick test、最终 pytest 和补丁导出。探针要求修复终态为 `fixed`、协调终态为 `released`，核对真实 pytest receipt，并通过 `git apply --check` 检查导出补丁。
- S7 契约测试覆盖任务绑定身份、backend/context 共享、启动失败清理、异常路径关闭顺序和不确定清理保护。

## 验证记录

- `pytest -q tests/test_sandbox_s7.py`：8 passed。
- 受影响回归：`pytest -q tests/test_sandbox_s7.py tests/test_repair_factory.py tests/test_bwrap_verify.py tests/test_repair_cli_lifecycle.py tests/test_bwrap_tool_routing.py tests/test_run_coordination_binding.py`：69 passed。
- 生命周期回归：`pytest -q tests/test_run_coordination_binding.py::test_finalize_closes_sandbox_before_releasing_owner tests/test_repair_flow_parity.py::test_exception_releases_resources tests/test_run_coordination_binding.py::test_session_open_failure_releases_fence_and_cancel_subscription tests/test_repair_factory.py`：9 passed。
- lease 修复回归：`pytest -q tests/test_collaboration_runtime.py tests/test_sandbox_s7.py tests/test_repair_factory.py tests/test_bwrap_verify.py tests/test_run_coordination_binding.py`：52 passed；`ruff check src/collaboration/repair_runtime.py src/orchestrator.py tests/test_collaboration_runtime.py` 通过。
- 心跳并发、owner 栅栏与异常关闭回归：`pytest -q -p no:cacheprovider tests/test_collaboration_runtime.py tests/test_sandbox_s7.py tests/test_repair_flow_parity.py tests/test_run_coordination_binding.py`：62 passed。相关源码、测试及探针 Ruff 通过；相关已跟踪变更 `git diff --check` 通过。
- Plan 环内快检新增回归：1 passed，验证实际调用、持久 operation 和独立最终验证不被替代；其余 Plan 与公开 CLI 修复链回归：26 passed、3 skipped（真实 Docker/GitHub/付费模型需显式 opt-in）、1 deselected（上述已通过用例）。相关 Ruff 通过。
- S6 最终发行版 API 闭环：[2026-10-11-sandbox-s7-probe-5.json](2026-10-11-sandbox-s7-probe-5.json)：`status=passed`、`repair_status=fixed`、`coordination_status=released`、最终 pytest `1 passed`，12 项检查全部通过。
- 实机环境：发行版 `FixLoop-Sandbox-s6-v1-83ecffc0a46c`，runtime digest `ffa81db422d58c3d21fa0674ff9c3465dd54bdf66bd03ff6ad4237d8529304af`，WSL2 kernel `6.18.40.1-microsoft-standard-WSL2`，bwrap `0.11.1`。
- 此前 probe-3 的 `recovery_required` 报告保留作为失败证据；根因是 collaboration task lease 过期，已由通用 heartbeat 修复。本轮没有运行全仓测试，也没有提交、推送或创建 PR。
- CLI 首次追加验收：[2026-10-11-sandbox-s7-probe-6.json](2026-10-11-sandbox-s7-probe-6.json)。修复与交付成功，但验收失败：默认 Plan 拒绝 Agent quick_test，且探针错误要求预期失败的基线 pytest 退出码为零。已分别修复 Plan 契约和探针断言；原始失败报告保留。
- CLI 最终实机验收：[2026-10-11-sandbox-s7-probe-final.json](2026-10-11-sandbox-s7-probe-final.json)：`status=passed`，20/20 项检查通过，CLI 退出码 0，`repair_status=fixed`、`coordination_status=released`。基线 pytest 退出码 1，成功复现预期失败；Agent quick_test 与独立最终 pytest 均退出码 0、`1 passed`。三次 pytest 持久 receipt 均为 `execution_status=completed`、`cleanup=confirmed`；backend 已关闭。补丁在任务 worktree 内生效，源仓库保持不变，导出的非空 `patch.diff` 通过 `git apply --check`。

## 验收结论

S7 的 API 与 CLI 主流程均已完成实机闭环：任务绑定、worktree 中的真实工具调用与 patch、Agent quick_test、独立最终 pytest、CLI 补丁交付、协调资源释放与 WSL2/bwrap backend cleanup 均通过验收；源仓库保持不变。CLI 最终验收使用全新的 fixture 与 state root，20 项检查全部通过。此次使用脚本化模型响应，没有调用外部 LLM API；实机验收验证的是沙箱与主流程执行链路，不包含真实模型的修复能力评估。
