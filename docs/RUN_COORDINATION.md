# 并发 Resume 与取消清理

同一 workspace/run 的执行权由 `agent_runtime/run_coordination` 的 SQLite 事务分配。
公开 repair 入口先获取 owner，再写 trace、协作任务和 Plan；竞争失败的 Resume 返回
`resume_owner_conflict`，不得改变赢家的任务版本、trace 或磁盘。

## 执行门禁

2026-10-02 增量见 [恢复与取消 MVP 验收](RECOVERY_CANCEL_ACCEPTANCE_2026-10-02.md)：严格 resume_run_id 不再退回普通执行，新执行指定身份使用 run_id；公开状态、进度和报告共享恢复/取消诊断。清理失败释放 owner 后，同 generation 重复 cancel 只返回已有结果，重新清理需显式恢复取得新 owner；已终结 run 使用 resume_run_terminal 区分活跃 owner 竞争。本轮相关证据单独记录，不覆盖下文历史 WSL 验收或开放新后端。

- 每次接管增加 `generation` 并更换 `owner_token`。旧 owner 的续租、派发、资源更新和终结均被拒绝。
- owner 默认租约为 30 秒，绑定每 10 秒续租；租约过期后不能自行续租。
- 接管先进入 `reconciling`，逐项核对旧 Plan attempt、AgentTask 和精确 sandbox call 收据，全部确认后才能进入 `active`。
- checkpoint 保存 coordination seal，包括 generation、owner、revision 和资源引用校验和。恢复校验历史 seal 与 owner acquisition lineage，不能通过改动 checkpoint 水位绕过门禁。
- WSL controller 与 supervisor 均在启动目标进程前校验 owner envelope；Windows transport 校验返回结果的 call、owner、generation 和 revision。
- PID 仅与进程创建标识一起用于判断原进程是否退出。PID 重用、未知身份、仅 lease 到期都不能作为清理成功的证据。

## 取消及恢复

`CancellationToken` 回调立即持久化 `cancel_requested`，关闭新工具派发。
取消按资源树从子到父处理：`AgentTask → PlanAttempt → SandboxCall`。
清理过程中即使某个 adapter 失败，也继续记录其他资源的结果。

| 证据 | 处理 |
| --- | --- |
| pending Subagent 已取消，尚未开始 | 确认 `not_started/cancelled`，拒绝后续 claim |
| 外部 Subagent running/expired，缺少终态 | 保持 unknown；phase 返回不能替它确认退出 |
| 精确 WSL terminal 收据，身份匹配、cleanup confirmed | 采用 completed/failed/cancelled 事实 |
| 持久化 start_failed 收据明确证明 no_target_started | 确认未启动，不永久阻塞恢复 |
| 目标、子进程、收据或写入结果未确认 | `recovery_required`，保留磁盘和 worktree |

父资源终结不能覆盖未知子资源。工具超时/取消返回未知写入时，不恢复快照，也不继续派发写工具。
墙钟超时不能把仍在运行的 phase worker 标记为结束。只有所有清理结果确认后，取消才允许回滚；
此时保留 `cancelling` owner fence，回滚完成后再释放 owner 并终结为 `cancelled`。
Subagent 的 cancel request 与 claim 在同一 SQLite 事务门禁中串行处理，不能因并发 claim 丢失取消请求。
入口或 Plan 初始化失败会显式释放 owner 和进程锁，不依赖垃圾回收。

Resume 遇到未确认资源时，公开 API 返回 `recovery_required`；不会启动模型或新工具。
旧 cancel request 在接管后继续清理；确认完成则返回 `user_cancel`，coordination 状态为 `cancelled`。
`released` 可以恢复；已经 `cancelled/failed` 的 run 不重新执行，应使用新 run identity。

## 定向验收

普通 Windows 测试覆盖 CAS 竞争、owner 崩溃接管、checkpoint 篡改、取消派发交错、
Subagent 状态、绑定初始化失败、续租、旧绑定关闭及 Windows 收据传输边界。
真实 Plan L2 案例在落盘后杀死修复进程，再从公开入口 Resume，断言写入只执行一次。

```powershell
python -m pytest tests/test_run_coordination.py tests/test_run_coordination_adapters.py tests/test_run_coordination_binding.py tests/test_wsl_launcher.py -q -p no:cacheprovider
python -m pytest tests/test_plan_l2_binding.py::test_public_repair_resume_after_process_crash -q -p no:cacheprovider
```

WSL 验收需要已有 Ubuntu/ext4 专用 fixture 和含 pytest 的 toolchain。
runner 复制当前源码到独立临时 controller；不降低 ext4 沙箱策略，结束时只清理自己创建的临时目录。
默认选择并发协调与沙箱生命周期用例；可传 pytest nodeid 只运行需要复验的案例。

```powershell
wsl.exe --distribution Ubuntu --exec /usr/bin/python3 /mnt/c/Users/haoyu/Documents/FixLoop/scripts/verify_run_coordination_wsl.py --fixture-base /home/haoyu/fixloop-sandbox-p0 tests/test_run_coordination_wsl.py::test_cancel_between_controller_and_supervisor_never_starts_target
```

真实 WSL 案例包括命令/pytest detached 子进程、double fork、controller 死亡、supervisor SIGKILL、
启动前取消交错、旧 generation 拒绝、历史精确收据及 workspace 隔离。
Windows transport 的单元验收使用模拟进程；native WSL 案例使用真实进程。
本功能不解除 `wire_orchestrator` 中已有的 `wsl_bwrap` CLI 配置门禁。

部分测试失败后按 `CLAUDE.md` 只复跑失败 nodeid；已有通过结果在未受后续改动影响时继续有效。
上述命令用于选择验收范围，不要求每次全部重跑；全量测试仅按用户授权执行。
