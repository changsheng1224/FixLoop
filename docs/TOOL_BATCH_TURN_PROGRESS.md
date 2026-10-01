# Native Tool Batch 与 Turn 实时进度

实现日期：2026-10-01。依据 [MVP 规格](superpowers/specs/2026-09-30-tool-batch-turn-progress-mvp.md)。

2026-10-02 增量：[工具批次预检 MVP](BATCH_PREFLIGHT_ACCEPTANCE_2026-10-02.md) 已实现冻结 schema 的逐项参数预检及可用 native 原始 content 的整批核对。参数错误项配对拒绝且不预约调用预算，合法兄弟继续；协议错误整批零执行。此增量单独记录相关验收，不修改下文历史 T1–T9 与固定批次实测数字。

## 使用与边界

native provider 一次返回多个 `ToolCall` 时，AgentLoop 自动建立调用批次。
无需模型指定 DAG、副作用类别或并发参数。模型结果按原始 ordinal 返回，
每项保留 provider 的 `tool_use_id`，包括失败、拒绝和取消结果。

- `read_file`、`list_files` 是首期并行白名单；最多四项候选、最多两路执行。
- 同名工具的不同参数使用独立 call/turn/batch 身份、上下文、取消 token 和收据。
- 超过四项或混入写、命令、测试、终态或未审计工具时，整批原序串行。
- 空/重复 ID、未知工具、非法参数结构在任何工具执行前明确报协议错误。
- XML 路径仍顺序执行；native 副作用路径保留单槽位在途 Action 与 uncertain 规则。

`grep`、`search` 可能启动外部 `rg`，不进入本次并行白名单；关系检索服务和
Observation 展开也不在白名单。白名单入口只执行受限 Python 文件 IO，保留
路径/敏感文件检查、扫描和输出上限、协作式取消。替换原 `run` 后自动失去
并行资格，运行时在批次建立时冻结注册表和默认路径解析器。

## 执行链路

`agent_loop._tool_step_flow` 将原工具步分成主线程准备、执行、主线程记录三个阶段。
准备阶段执行修复预算与收敛规则；worker 使用 `ToolExecutor.for_call` 的独立 facade，
继续经过 Gateway 和完整九道 Executor 闸口。worker 不写 owner session、TaskState、
模型 history、编辑锁、checkpoint 或 Plan journal。

共享配额先原子预留、后确认消耗；闸口拒绝释放预留。重复窗口加锁，resilience
沿用已有线程安全控制器。主线程按 ordinal 存储 Observation、更新预算和编辑锁、
归并模型结果。参数 hash 和幂等键包含调用身份；同名调用不共用收据槽位。
文件读取在执行前后及归并前复核版本，变化的结果标 `partial/stale_precondition`，
不能作为已验证 Action 或编辑锁读取授权。

`read_permits` 按 workspace/run 共享两路容量。PlanScheduler 的探索节点持有父 permit，
其内部 native batch 每次借用一条容量；两个 Plan 节点不会再各开两条读取。
借用通过引用计数管理：父节点返回时，如果子读取仍存活，容量继续占用。
单独运行的 native batch 可以使用两路。Plan 的 operation 预算和工具权限仍照常检查。

Plan operation 在 AgentLoop owner 上准备并在调用完成时记录可信收据；模型结果仍
按原序归并。operation 保存 provider call ID、batch/turn ID 和 ordinal。允许多个
独立读取基于同一未变化的前置 workspace 版本准备。

## 取消、事件与恢复

取消先关闭新派发；排队调用形成独立 cancelled 结果。运行调用收到自己的 token，
默认最多等 250ms 清理。worker 实际返回后才确认结束；超过期限标 uncertain，
丢弃迟到结果，并把容量保留到线程实际退出。不能用 Future.cancel 或非等待 shutdown
作为清理证明。无法确认清理时设置既有 execution_uncertain 闸门，阻止后续副作用。

工具超时使用同一取消与清理边界。收集 Future 时再次读取取消 token，覆盖取消
刚发生、主循环尚未更新局部标记的竞态。未确认占满本批容量时停止排队派发。

事件先写 canonical trace，再投递 `on_turn_progress`。每个 Turn 有单调 `event_seq`；
包含 Turn 开始/阶段/结束、batch 建立/结束和 call 排队/开始/完成/取消/uncertain。
阶段来自已有 ReAct 通知。事件只含安全身份、状态、原因、耗时和错误码，
没有参数、源码、命令全文或模型推理。

CLI 输出阶段、工具名及短 call ID、running/queued/done 数量。`TurnProgress` 按实体
去重，乱序事件不回退状态；`replay_progress` 产生与现场相同的投影。
checkpoint 单独保留活动 batch/call 收据引用和事件序号，不受最近 100 条 Action 截断影响。

`restore_progress` 用可信 Plan operation 收据覆盖显示投影；日志缺失/损坏只标
`progress_replay_incomplete`。恢复 CLI 通过 `on_turn_progress_replay` 显示已确认和
未确认数量。显示投影不产生合成执行事件，也不授权重放工具。

**恢复边界**：已有 Plan journal/recovery 是执行事实权威。无 Plan 时没有新增批次
执行 journal，不承诺崩溃中完整续跑；旧执行需先确认停止，再由新 Turn 重新读取。
未知副作用继续走原 Action/Plan uncertain 规则，不从 UI 状态推断成功或自动重放。

## 验收证据

| ID | 覆盖与证据 |
|---|---|
| T1 | `test_native_tool_batch.py`：同步屏障、真实文件读取、同名参数、Observation/Action/收据及 provider 结果逐项配对。 |
| T2 | `test_tool_batch.py`：四调用屏障、峰值 2、waiting_capacity；native 配额和修复预算竞争；`test_tool_batch_plan_integration.py` 证明两级共享上限。 |
| T3 | batch 与 native 测试：空/重复 ID、未知工具、非法 JSON 结构，无执行。 |
| T4 | 独立失败和路径权限拒绝不阻断兄弟调用，模型结果保留原序。 |
| T5 | native read → write → read 观察到原内容与新内容；副作用工具仍走原闸口。 |
| T6 | 同步屏障取消、排队不执行、协作退出、迟到丢弃、超时、借用 permit 保留及取消收集竞态。 |
| T7 | `test_turn_progress.py` 与 native trace：现场/重复/乱序回放一致，事件不含源码。 |
| T8 | checkpoint 截断和实际 step resume；`test_native_tool_batch_crash.py` 使用真实 `os._exit(73)`，读取收据决定确认展示，写 dispatched 后保持 uncertain、不重放。 |
| T9 | `test_native_tool_batch.py` 与演示脚本通过生产 AgentLoop native API，运行中 CLI 可见 started，配对结果送入下一模型轮。 |

测试使用离线 provider fixture，不访问外部模型 API。既有 AgentLoop、ToolExecutor、
tool runtime contracts、canonical trace、Plan、checkpoint 和 callbacks 相关测试一并验证。
只运行相关测试；没有运行全量测试或发布验证。

最终新增验收套件：41 passed（16.23s）；原有工具测试：30 passed（7.58s）。
受影响的 21 个 Python 文件通过 Ruff lint 与 format 检查，`git diff --check` 通过。

## 固定批次实测与复现

```powershell
python scripts/demo_tool_batch_progress.py --output .tmp/tool-batch-final-demo
```

脚本生成四个临时 Python 文件，通过真实审计读取入口执行，在每次读取前添加可控
50ms 延迟。离线 provider 一次返回四调用，再消费结果并结束。输出保留 CLI 文本、
trace 路径、时间区间、预算、Observation、收据和摘要 JSON。

Windows / Python 3.14.3 实测（含上下文、Observation、checkpoint 等运行时开销）：

| 指标 | 串行 | 并行 |
|---|---:|---:|
| 总耗时 | 0.9320s | 0.4729s |
| 首工具开始至末工具结束 | 0.3522s | 0.1299s |
| 实际读取峰值 | 1 | 2 |
| 工具调用消耗 | 4 | 4 |

结果内容、调用预算和配额一致，两条路径的回放均与现场一致。该样本含可控延迟，
不推断其他工具、环境或真实模型调用的提速幅度。
本次本地证据目录：`.tmp/tool-batch-final-demo/e3a07b178e56/`。

## 项目复盘素材

核心问题是调用身份和状态的所有权：同名调用必须用 provider call ID 识别，
不能用工具名或共享 session 单槽位；单纯把旧 `_run_tool_step` 放入线程池会串
参数、收据和取消状态。解决方式是主线程准备与归并、上下文显式传入、共享资源
原子预留，worker 只产生结果。

关键验证包括：屏障证明重叠；取消刚好发生在 Future 收集时的回归；父 Plan 返回
后迟到子线程仍占容量；进程在收据落盘后但完成事件之前退出。最后一项说明 UI
事件不能替代执行 journal，恢复必须遵循可信收据和实际退出证据。
