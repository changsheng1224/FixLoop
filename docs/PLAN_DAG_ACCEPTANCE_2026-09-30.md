# Plan DAG MVP 验收记录

日期：2026-09-30（本地 Windows 工作区）

本报告记录 FixLoop Plan 与在途恢复 MVP 的选定 Python / host pytest 路径验收。测试使用固定 `FakeModelClient` 输出，不调用网络模型 API；文件修改、pytest、SQLite journal 和进程级退出均真实执行。

## 结果

相关回归命令（包含边界补强前的完整相关回归；补强后的新增边界回归另行执行）：

```text
pytest -q tests/test_plan_runtime.py tests/test_plan_crash_windows.py tests/test_plan_l2_binding.py tests/test_tool_runtime_contracts.py tests/test_tool_executor.py tests/test_context_runtime_governance.py tests/test_observation_store_governance.py tests/test_checkpoint_resume.py tests/test_strong_step_resume.py tests/test_checkpoint_trigger.py tests/test_agent_loop.py tests/test_cli_repair.py tests/test_repair_factory.py tests/test_verifier.py tests/test_resume_repair.py -p no:cacheprovider --basetemp=.tmp/plan-final-validation --junitxml=.tmp/plan-final-validation.xml
```

结果：`215 passed, 1 skipped`，耗时约 311 秒。随后执行 `test_workspace_lease_is_independent_of_state_root` 和 `test_read_cancellation_propagates_while_tool_is_running`，结果 `2 passed`。未运行全量 `pytest tests/`。

静态检查：`ruff check` 通过；`ruff format --check` 通过；`git diff --check` 通过。

## D1-D15 映射

| ID | 验收证据 |
|---|---|
| D1 | `test_graph_rejections` 覆盖重复 ID、未知依赖、环、完成条件、身份和节点预算 |
| D2 | `test_graph_rejections` 的只读工具副作用拒绝；可信 registry 校验 |
| D3 | `test_two_reads_have_atomic_budget_and_owner_reducer`，最多两路并行和原子预算 |
| D4 | `test_serial_nodes_cannot_be_prepared_concurrently`，edit/verify 由 owner 串行 |
| D5 | `test_success_without_completion_blocks_downstream`、`test_actual_write_and_tests_are_required` |
| D6 | `test_one_parallel_read_failure_does_not_discard_other_result` 及 blocked 传播 |
| D7 | 外部文件变化、过期 Observation 和已写后态变化测试 |
| D8 | `test_replan_preserves_history_is_bounded_and_atomic`，两次上限和旧版本保留 |
| D9 | `test_write_crash_cut_points`、真实子进程退出切点测试 |
| D10 | `test_process_exit_recovers_durable_facts`，可信 durable 结果只接纳一次 |
| D11 | `test_partial_write_is_not_replayed`、收据完整性和 uncertain 状态 |
| D12 | 并行读取取消/失联测试；子 token 独立且主取消可传播 |
| D13 | `test_interrupted_verifier_requires_real_stop_evidence`，无清理证明不重启 |
| D14 | journal/checkpoint/receipt/workspace 身份不匹配拒绝恢复 |
| D15 | `test_actual_l2_tools_and_pytest`、公开 repair/resume 和进程中断续跑 |

## 真实 L2 记录

公开入口验收证明同一个 `repair_run_id` 可续跑；进程级中断后公开续跑的结果为 `fixed`，恢复时模型调用为 `0`，实际写调用为 `1`。另一个中断 fixture 在补丁已落盘、Plan 尚未完成时恢复并接纳 `edit`，随后只执行一次 host pytest 验证；记录位于 `.tmp/plan-acceptance/` 对应 fixture 的 `.agent/plans/*/acceptance.json` 和 `public_acceptance.json`。

## 边界

未知写入、部分写入、缺失或损坏收据、无法证明旧验证/子进程停止，以及工作区外部变化都会保持 `uncertain` 或拒绝恢复；系统不会盲目重放写入。checksum 用于完整性和关联检查，不是防恶意篡改的签名。工作区锁在同一宿主机临时目录按 workspace ID 共享，跨宿主机不提供锁保证。没有做断电、存储设备故障、恶意仓库、沙箱增强、性能收益或修复率对照实验，也不宣称 exactly-once。
