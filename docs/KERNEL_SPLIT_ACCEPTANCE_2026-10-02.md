# 内核职责拆分 MVP

日期：2026-10-02。范围：用户确认的工具批次编排与 Observation 记录拆分。承接 [统一请求准备](CONTEXT_ASSEMBLY_ACCEPTANCE_2026-10-02.md)、[证据消费](EVIDENCE_CONSUMPTION_ACCEPTANCE_2026-10-02.md) 和 [决策恢复投影](DECISION_PROJECTION_ACCEPTANCE_2026-10-02.md)。本轮是局部职责拆分，P3 的其余目标继续单独讨论。

## 本轮实现与职责

| 模块 | 本轮职责与接入 |
|---|---|
| [AgentLoop](../agent_runtime/agent_loop.py) | 协调模型回合、工具步骤、修复策略与终态；通过明确回调连接下述模块 |
| [ToolBatchRunner](../agent_runtime/tool_batch_runtime.py) | 创建批次，连接准备、执行、收集与原序归并，返回模型结果块和每个 call 的 Observation 引用；维护批次 checkpoint，关闭尚未结束的步骤/Plan 操作 |
| ToolBatchScheduler / ToolExecutor | 沿用容量、取消、期限、读许可和执行闸口；Runner 复用这些实现，未新建调度器 |
| [ToolObservationRecorder](../agent_runtime/observation_recording.py) | XML/native 工具步骤共用：结果规范化、依赖失效、事实/来源信息入库、匹配 action 的引用关联、观察事件和短历史投影 |
| PlanSession / journal | 继续提交操作事实和收据、执行恢复对账；批次模块通过显式 Plan 操作回调接入 |

新增模块接收 session、ToolContext、执行器、进度器、期限/取消信号以及限定用途的回调，不接收整个 AgentLoop，也不转发其私有字段。`BatchExecutionHooks` 给出回调类型，`SettledToolStep` 返回内容和明确的结果引用，Runner 不通过“最后一次结果”猜测 call 对应关系。

当前 Loop 接线仍从既有工具步骤取得刚记录的 Observation ID，将它封装后返回；工具步骤内的预算、action 状态转换与修复判断留在原处。首版没有一次性搬走约 800 行步骤流程，也没有将所有策略下沉到 L2。

## 执行与记录次序

```text
主循环准备模型请求 → 模型给出工具调用
  → owner 创建批次、准备步骤与 Plan 操作、冻结执行器快照
  → 现有执行闸口（审计只读调用可在 worker 执行，其他批次串行）
  → owner 收集结果、复验读取、提交 Plan 操作收据
  → owner 按 ordinal 归并：存储 Observation → 关联 action 引用
  → 更新进度/checkpoint → 返回 call ID 配对的模型结果
```

- 模块入口绑定创建它的线程。Loop 在 `run()` 入口创建本次记录器，owner 是实际运行线程，不要求等于 Loop 构造线程。并行 worker 不能调用记录服务或运行整个批次。
- owner 线程检查只是本进程的调用边界，不代替 Plan generation fence、角色授权或持久 owner 身份。回调是可信运行时接线，未增加模型可配置的插件入口。
- 收据和工具完成事件可能早于 Observation 归并。`tool_call_completed` 表示工具执行结果，不证明 Observation 已持久化，也不是恢复授权；执行事实仍以 journal/收据和后态对账为准。
- queued 取消没有执行/预算预约，也会产生原序结果和 Observation。执行终止未确认时保留 uncertain；迟到的 worker 输出不进入当前结果块。
- Observation 的原文、结构化事实、源码依赖、检索契约、MCP 来源与脱敏仍走原来的 Store。非 patch 工具引起的源码变化继续通知探索失效，relations 来源关联通过限定回调接入。
- Store 在持久化失败时同样关闭；写入失败不发布新的 Observation ID、action 结果引用或 observation_stored 事件。失败仍向调用方传播。

多层存储和回调没有新增原子提交保证。Plan 收据可能已提交，而后续 Observation 存储失败；这是需要诊断的阶段差异，不可据此盲目重放副作用。冻结结果包装只固定字段归属，不承诺嵌套记录深度不可变。

## 借鉴与技术取舍

| 参照 | 本轮采用 | 保留或后置 |
|---|---|---|
| Pi（R-02/R-05） | 循环协调、上下文准备、工具执行与结果归并分别负责；使用明确依赖和回调 | 延续 Python 与现有协议；不复制默认并行策略、不引入跨语言内核 |
| OpenCode（R-06/R-10） | 服务职责与展示事件分开；权限仍由可信执行层承担 | 不新增服务端/SSE、持久层或能够绕过闸口的插件系统 |
| Hermes（R-08/R-09） | 历史/经验能力仍应在稳定的上下文与执行边界上接入 | 本轮不增加历史索引、经验提炼或 Skill 自动发布 |

来源和原有差异分析见 [五维参照](AGENT_DESIGN_REFERENCES_2026-10-01.md)，本地决定见 ADR-018。具体模块、回调和存储次序是 FixLoop 的实现，不声称上游提供相同执行保障。

本轮保留两个协议循环和生成器式工具步骤，降低已有取消/恢复窗口的迁移风险。patcher 收敛、定向重读、补丁恢复、repair 专属预算与终态策略继续由当前 Loop 接线；L1/L2 全面解耦未完成。**两个新增模块无 `src` 依赖，现有 AgentLoop 的 edit-lock/grounding 等 `src` 依赖仍存在。**

## 验证记录

相关节点按最新运行结果去重：**127 passed、0 failed、0 skipped**，其中 [新增模块行为测试](../tests/test_kernel_split.py) **9 个节点**。使用受控 provider，实际磁盘工具、host pytest、worker 线程和子进程退出；未调用在线模型，未运行全量测试，未提交/推送或创建 PR。

| 报告 | 本批结果 | 验证与处理 |
|---|---|---|
| `first.xml` | 16 passed / 4 failed | native 批次、Plan 共享许可、checkpoint 与进程退出；构造/运行线程差异和旧崩溃夹具缺失任务上下文由后续复验 |
| `fixes.xml` | 3 passed / 1 failed | 文件版本复验与两种真实进程退出通过；取消夹具因冷启动超过原三方屏障期限失败 |
| `cancel.xml` | 1 passed | 取消夹具等待两个工具实际启动后再取消；不依赖模型初始化耗时 |
| `services.xml` | 100 passed / 1 failed | 模块行为、AgentLoop、Scheduler、代码证据消费、两种 L2 委派→owner 重读→写入→host pytest；新 MCP 测试误将 Store.expand 的字符串当作对象 |
| `mcp.xml` | 1 passed | MCP 原文、来源和精确 action 引用关联，修正测试返回类型假设 |
| `cleanup.xml` | 2 passed | 准备失败关闭已有操作；未确认超时保留 uncertain 与每个结果引用 |
| `contracts.xml` | 6 passed | 精确复验返回引用的新增测试接线、清理与 MCP 场景 |
| `recovery.xml` | 4 passed | XML/native 确认补丁后旧决策失效不重放、最新版决策恢复、历史输入确认后态 |

原始报告、[汇总脚本](../artifacts/kernel-split-mvp-2026-10-02/summarize.py) 和 [summary.json](../artifacts/kernel-split-mvp-2026-10-02/summary.json) 保留在 [artifacts](../artifacts/kernel-split-mvp-2026-10-02/)。脚本按 JUnit 开始时间选择唯一节点最新结果，避免并行验证批次的结束先后覆盖后一次复验；不直接相加重复通过数。

崩溃夹具补齐显式任务，并设置 6000 输入预算/硬顶，让完整 native schema 与必需内容通过既有门禁后抵达故障点；生产预算与硬顶未修改。取消夹具保留两个工具的并发屏障，用实际启动事件等待运行时初始化。初次生产错误是将构造线程误作运行 owner，已修复；其余为夹具契约或测试返回类型问题。

受影响 7 个 Python 文件 Ruff check/format check 通过；相关已跟踪差异的 diff check、67 个本地文档链接、新增文件空白及新增模块的 `src`/AgentLoop 导入边界检查通过。此验收不代表在线修复率提升、全仓 Layer 1 无 `src` 依赖或跨存储 exactly-once。
