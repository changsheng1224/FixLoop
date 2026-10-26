# LangGraph 阶段图与正式接入（D3 / D4）

日期：2026-10-26。边界见 [D1](LANGGRAPH_ORCHESTRATION_BOUNDARY.md)，调用方式见
[D2](LANGGRAPH_EXECUTION_D2.md)。本文件记录本次实现与实际验证，不代替后续完整 D5 验收。

## D3：阶段图

- `src/repair/graph.py` 定义全部节点和条件边；图数据只有临时 `next_action`。
- `src/repair/graph_stages.py` 在 owner 上调用既有服务，维护原阶段计时与事件。
- `src/repair/attempt_policy.py` 提取原空补丁、Critic 和验证失败策略，不改变反馈、
  回滚、计数、止损和 checkpoint 的先后关系。
- `src/repair/graph_owner.py` 将 D2 已验证的调用桥移入生产模块；实验测试改为验证
  同一份实现，删除原测试专用副本。生产模块不导入 tests。
- `RepairState` 与 `ExecutionSession` 通过 runtime context 传入同一实例；不配置
  checkpointer、Store、缓存、节点自动重试或框架超时。
- 图步数保护按剩余尝试额度计算，覆盖大于框架默认步数的合法修复请求。

完整拓扑由实际编译图导出为 [LANGGRAPH_REPAIR_FLOW.mmd](LANGGRAPH_REPAIR_FLOW.mmd)。
可重新生成：

```powershell
python -c "from pathlib import Path; from src.repair.graph import repair_graph; Path('docs/LANGGRAPH_REPAIR_FLOW.mmd').write_text(repair_graph().get_graph().draw_mermaid(), encoding='utf-8')"
```

“验证失败 → 回滚并记录诊断反馈 → 重试或结束”是独立的 `verification_failure`
节点。扩展节点时增加对应阶段方法和 `STAGE_ROUTES` 连接即可；条件边只读路由。
涉及新业务判断、工具调用或持久化边界的扩展仍需单独评审，不允许绕过内核。

### D3 定向验证

同一工作区隔离解释器执行：

```powershell
.runtime-tools/langgraph-d2/Scripts/python.exe -m pytest tests/test_repair_graph.py tests/test_langgraph_execution.py -q -p no:cacheprovider --basetemp=.runtime-tools/langgraph-d2/pytest-d3
```

结果：22 passed。覆盖正常／恢复入口、原 State 与 owner 身份、Critic 拒绝、
明确空补丁终态和重试、接口保护先于验证开关、验证失败的回滚与当前账本反馈、
非测试结果、累计阶段超时、预取消，以及 300 次合法业务重试。
其中 D2 继承的 invoke／stream 测试实际执行文件修改、SQLite 和 pytest。

## D4：正式入口

`RepairService.run → ExecutionLifecycle → RepairCoordinator → run_repair_graph`
成为正式调用路径。CLI、Workbench 和评测继续构造原有请求，不增加入口或兼容开关。

- Coordinator 删除 `run_repair_attempts` 和已移入 `RepairAttemptPolicy` 的四个策略
  方法，不保留第二套阶段调度循环。原恢复加载、启动事件、超时映射、取消处理、
  正常结果收尾及 finally 资源清理继续包围图调用。
- 原 FSM、Plan、checkpoint schema、模型提示、工具权限、预算和审批协议均保留。
  等待审批从图阶段抛回原异常映射，返回 `waiting_approval`，仍由原入口恢复。
- LangGraph 1.2.17 提升为默认依赖，`.[dev]` 即可安装正式修复能力；删除 D2 的临时
  orchestration extra。普通 Agent／RepairService 的模块导入仍不主动加载阶段图。
- CI 用默认依赖执行原轻量门禁，并增加阶段图、owner 和正式入口测试；尚未远端执行。

### D4 定向验证

本地环境仍为 Windows x64 / Python 3.14.3 / LangGraph 1.2.17。均使用
`.runtime-tools/langgraph-d2/Scripts/python.exe`，并关闭 LangSmith tracing 和远程模型调用。

| 测试范围 | 结果 |
| --- | --- |
| `test_repair_flow_parity.py`、`test_plan_l2_binding.py`、`test_resume_repair.py`、`test_verification_transactions.py` | 70 passed |
| `test_repair_graph_entry.py` 当时四个 fresh/resume 用例，及 `test_phase_c_timeout.py`、`test_run_coordination_binding.py`、`test_recovery_cancel_outcome.py`、`test_sandbox_s7.py` | 53 passed |
| 新增共享编译图的双会话并发隔离用例 | 1 passed，真实文件和真实 pytest |
| CLI 参数／生命周期、repair factory、Workbench 真 L2 修复、EvalRunner 离线两例，以及真实 CLI worktree 交付 | 最终 37 passed、1 个既有 skip；见下述短目录复验 |
| `scripts/wheel_smoke.py` | 独立源码构建、wheel 安装后离线 case_001 / case_002 均通过 |
| 默认依赖 editable 安装与安装元数据 | 安装成功，Requires-Dist 包含精确版本 langgraph，`pip check` 通过 |
| 新增／修改的八个 Python 文件 | Ruff lint 与 format 检查通过 |

调用端批次首次是 36 passed、1 skipped、1 failed。失败案例的 `delivery/result.json`
明确记录 WinError 206：过长的测试 basetemp 导致创建原有 runs 目录失败，发生在阶段
执行前。只复验该 nodeid，使用较短工作区目录，结果 1 passed：

```powershell
python -m pytest tests/test_run_entry_isolation.py::test_real_host_pipeline_cli_delivery_uses_candidate_checkout -q -p no:cacheprovider --basetemp=.tmp/d4-cli
```

没有为此改动路径处理、状态协议或资源核查。既有 skip 为
`test_cli_repair.py::TestCLIRepair::test_repair_skip_verify_smoke` 的预先声明跳过，
其他 CLI、Workbench 与评测路径已实际执行。

D3/D4 共 183 个不同的相关用例通过，另有上述 1 个既有 skip；未运行全量测试、
真实模型评测、WSL／Docker 或远端 CI。没有据此声称速度、Token 或修复率提升。
后续 D5 仍可按实际需求补充发布级／其他平台验证，本次未将其标记为完成。
