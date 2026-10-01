# Subagent 只读探索 MVP 验收记录

日期：2026-10-01。实现分支：`codex/subagent-readonly-exploration`。

## 接入与行为

`create_repair_agent("patcher")` 注册 `delegate_exploration` / `collect_exploration`，由 `RepairPlanBinding` 在真实 L2 run 内安装 `ExplorationRuntime`。共享 Layer 1 前缀保持原契约，新增工具说明属于主 Agent 的 Layer 2 角色上下文。Layer 1 无 `src` 导入。

一次委派 1–2 个独立任务，仅支持实现定位和相关测试发现。整批校验、预算预留和 SQLite 事务创建；稳定句柄绑定父 task/run。每任务独立 Agent、上下文、取消 token、模型客户端状态和 Observation session，最多 3 次模型调用 / 4 次只读工具调用。两路 worker 与 Plan / native Tool Batch 共用每 workspace/run 的两枚只读 permit，模型和工具成本计入父 run 的预算。未知 usage 保守结算，durable attempt 收据用于恢复和幂等预算重建。

Explorer 仅有 `list_files`、literal `grep`、`read_file`，注册表及执行闸口均拒绝写、shell、测试和递归委派。来源绑定运行时生成的 Observation checksum、文件完整 hash、路径范围及 attempt；模型无法自行提交文件版本。collect 重验 workspace、Plan、来源与完整性，stale / partial 不进入成功发现。完全重复合并来源，不一致陈述标记待复核。主 Agent 回收后重新读取来源，才能记录自己的 Plan evidence / 决策；子任务陈述仍是候选。

修改、测试、回滚前有子 worker 退出门禁。父取消停止派发并有界等待；未退出的线程保留 permit，状态为 worker_lost，阻断后续修改。旧 attempt 的结果和进度均被 generation fence 拒绝。恢复必须先确认旧进程退出或清理收据，才创建新 attempt。初始化期间也维护 owner 心跳，避免慢 Plan 初始化耗尽入口租约。

## 验证证据

`tests/test_exploration_runtime.py`：40 项通过，覆盖 A1–A9 的并行屏障、隔离、双闸口、事务回滚、预算、来源篡改、版本变化、截断、去重、取消、迟到结果/事件、真实进程崩溃及恢复。新鲜结果重复 collect 和重复恢复排队任务保持幂等。

`tests/test_exploration_l2.py`：文本和 native 主 Agent 均走实际 Agent/model/tool/Plan/disk 修复路径，主 Agent 委派两个探索、回收后重新读取来源、记录两条 Plan 决策、独自执行一次文件修改，并由实际 pytest 验证一个测试通过。另有短租约初始化测试。模型响应由受控 ModelClient 提供，未模拟 AgentResult；测试会生成 `subagent_acceptance.json`，保留 Plan、任务、来源、owner review 和事件，原始 Observation / Plan journal 在同一 fixture 的状态目录。

相关既有回归首次运行 83 项通过、2 项失败；修复初始化心跳后，公开入口进程崩溃恢复用例通过。另补验 owner 初始化失败、关闭、取消和 stale owner 释放的 8 个用例，全部通过。另一失败用例 `tests/test_patcher_toolized_edit.py::TestPatcherToolizedOrchestrator::test_toolized_path_uses_disk_diff` 的旧 fixture 用 MagicMock Agent 生成了无效 state_root，并在运行前预改文件、模拟快照。已改为真实 Agent、受控模型响应、实际工具执行和磁盘快照，核验完整 pre/post image、导出 diff 和唯一成功的 Plan 写入收据；精确用例复验通过（18.72 s），生产代码未改动。两项失败均已修复并复验，已通过且未受影响的相关结果保持有效。

受影响 Python 文件 Ruff check / format 和 `git diff --check`通过。按 CLAUDE.md 仅运行相关测试，未运行全量测试，未提交、push 或创建 PR；用户原有未跟踪文件保持原样。

## 固定任务测量

[机器可读记录](2026-10-01-subagent-readonly-measurement.json)来自 `test_fixed_task_serial_parallel_measurement` 的一次实际运行：同一实现/测试任务，比较相同 Explorer 循环串行执行和同时委派两路，受控 provider 每轮延迟 50 ms。以下 token 为受控客户端报告的 usage，无法用于推断在线模型成本。

| 指标 | 串行 | 两路并行 |
|---|---:|---:|
| 实测耗时（ms） | 450.31 | 282.83 |
| 模型调用数 | 4 | 4 |
| 只读工具调用数 | 2 | 2 |
| 报告 token | 600 | 600 |
| 模型调用并发峰值 | 1 | 2 |
| 来源验证有效的候选 | 2 | 2 |
| 重复发现 | 0 | 0 |

两种方式均发现 `value.py` 与 `test_value.py`，有效来源为 2/2，并行额外报告 token 为 0。来源有效率不等于陈述语义正确率、测试覆盖率或线上修复成功率。未评测在线模型延迟/费用、真实大型仓库收益、任意子任务 DAG、递归委派、语义冲突裁决或跨任务缓存。
