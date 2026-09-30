# FixLoop Plan 与轻量 DAG

实现入口：`src/cli.py repair`。Repair 强制使用证据驱动的 Plan DAG；旧的无 Plan repair 流程已移除。L1 通用模块位于 `agent_runtime/plan_runtime/`，不依赖 `src`；L2 `RepairPlanBinding` 将同一个 PlanSession 绑定到 Patcher、Orchestrator 的回滚/重试和最终 Verifier。

```bash
python -m src.cli repair --repo ./repo --issue "问题描述" --execution-tier host
python -m src.cli repair --repo ./repo --issue "相同问题描述" --execution-tier host --resume-repair <run-id>
```

当前端到端验收覆盖 Python / host pytest 路径。静态检查没有执行测试，不能满足 `tests_passed`；其他验证后端缺少完整的命令、测试计数或终止收据时也不自动确认成功。`--skip-verify` 会保留未完成的 verify 节点。

## 规划与执行

同一 Patcher 先执行固定、受预算约束的只读操作并保存 Observation，然后由其已有 light client 或主模型进行一次受约束 JSON 规划。输入包含任务、真实证据 ID、文件版本和 Observation 摘要。候选可以修改小图；未知位置需安排 explore 节点。候选必须保留 analyze → edit → verify 的依赖关系。非法图拒绝；顺序候选也走同一校验器，缺少结构化分析结论时不开启写入。

每个节点有显式依赖、可信工具白名单、有限类型的完成条件和证据引用。`observation_present` 核对持久 Observation 与文件版本；`analysis_recorded` 要求结论及有效引用；`patch_applied` 要求终态收据和当前文件后态；`tests_passed` 要求完成的测试命令、通过结果及非零测试计数。记录分析并不保证分析正确。

上限为 8 个节点、每节点 3 个依赖、两路只读探索、每任务两次重规划。预规划只读调用至多 4 次；L2 探索还共享 4 次读预算，整个 Plan 具有持久计数的 50 次工具调用上限。计划生成输出预算 1,800 tokens，单次等待最多 60 秒且不超过已有 repair deadline。

调度器仅并行固定 explore 操作。子操作重建工具注册表，使用独立 ToolContext、session、取消 token 和子配额；主任务取消实时传递给子 token，单个子操作取消不会取消兄弟操作。共享预算原子预留，主线程归并状态。分析、修改与验证由持有 workspace lease 的主线程串行处理。工具分类来自受信注册表，模型不能把 shell、测试或写工具声明为 read。

`plan_version` 只在图更新时递增，`state_revision` 表示状态快照。reducer 是节点状态修改入口，拒收旧版本/旧 attempt 的结果。失败和不确定向下游传播 blocked。写入使旧探索/分析证据 stale，但已经发生的、收据支持的 edit 不因此重新派发。最终叶节点成功可以结束计划，历史前置读证据仍保留 stale 状态。

Orchestrator 原有重试控制器保留。验证失败后的回滚也先记意图、再记录实际后态；确认回滚后才能提交新图，并把之前 edit 的操作历史保留在旧版本中。未确认的回滚和活动/uncertain 节点阻止重规划。新图提交是原子的，不允许通过修改稳定 node ID 偷换语义。

## 持久化与恢复

存储布局为 `.agent/plans/<task/run 摘要>/journal.sqlite3` 和内容寻址 `blobs/`。已有 `state_root` 配置可将记录放到工作区外。SQLite 使用 WAL、`synchronous=FULL`；每条追加事件带单调序号、前项 checksum 和自身 checksum。模型/工具结果中的 receipt 身份也单独校验。Plan 保留自己的 Observation 元数据和受校验的原始证据副本，避免原有 GC 或 100 条历史窗口删除在途依据。

派发顺序：prepared attempt → running revision → dispatched attempt → prepared/dispatched tool operation → durable tool result/Observation → durable node result → reducer revision → reconciled attempt → checkpoint seal。故障注入点直接位于这些写盘边界。L1 和 L2 checkpoint 都封装 Plan seal；恢复检查 seal 的身份、历史序号、Plan revision 和活动 attempt 的反向关联，允许 journal 前滚。普通 L1 step-resume 遇到 Plan checkpoint 会要求从 L2 Plan 入口恢复。

恢复使用 OS 文件锁、进程 generation 身份、终止证明、收据和文件内容 hash。锁存于同一宿主机临时目录的 `fixloop-plan-leases/<workspace-id>.lock`，同一个工作区配置不同 `state_root` 仍然互斥；不提供跨宿主机锁。prepared 未派发的节点可安全重新准备；已经落盘但未更新 Plan 的可信结果可以接纳一次。只有确认旧只读/验证执行停止，才能创建新 attempt。已成功写入的后态不匹配、收据缺失、部分写入、执行清理未知或 checkpoint 身份不符时，保持 uncertain 或拒绝恢复，不重放写入，也不开放冲突操作。

`recover(session, stopped_probe=...)` 允许执行后端提供受信任的停止/进程树清理证明。默认只有固定本地文件读可以用旧 owner 退出作为停止依据；未收齐结果的 grep/LSP/测试等潜在子进程不能仅凭父进程退出自动重启。宿主机旧 pytest 进程树无法核实时会停止在 uncertain，这是恢复判定的一部分。先核查实际 diff 与进程，再由调用方提交有依据的恢复结果或新图。

完整性校验用于检测损坏和关联错误，checksum 不是防恶意篡改的签名。host pytest 仍沿用仓库的 trusted-host 执行契约；对不受信任的项目执行需使用具备隔离和可信 state_root 的后端。本实现没有增加宿主机沙箱。

Windows 上已实跑进程退出、SQLite 事务和写句柄 fsync；没有模拟断电、存储设备故障或恶意仓库破坏。这里保证执行事实可核查，不声称 exactly-once。

## 验收

`tests/test_plan_runtime.py` 覆盖图/权限/证据/预算/重规划/完整性；`tests/test_plan_crash_windows.py` 覆盖写盘切点、部分写入和真实子进程退出；`tests/test_plan_l2_binding.py` 覆盖实际 Agent 工具编辑、host pytest、公开 repair 入口、回滚重规划和进程中断后公开续跑。

模型输出使用 FakeModelClient 以固定控制流；文件修改、pytest、SQLite 和进程中断均真实执行，不使用网络模型 API、答案补丁或历史运行冒充本次验收。恢复后的公开修复任务只发生一次写调用，续跑不调用模型，最终验证实际通过。没有运行 Todo/DAG 效果对照，也不宣称速度或修复率提升。

验收记录见 [本地验收报告](PLAN_DAG_ACCEPTANCE_2026-09-30.md)。仅运行改动相关测试，未运行全量测试。
