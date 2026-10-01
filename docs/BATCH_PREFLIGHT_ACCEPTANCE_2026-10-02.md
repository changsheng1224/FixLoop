# 工具批次预检 MVP

日期：2026-10-02。范围：用户确认的批次派发前预检与 native 调用完整性。承接 [内核职责拆分](KERNEL_SPLIT_ACCEPTANCE_2026-10-02.md)，本轮不扩大并行白名单、不增加 XML 批次或重试控制器。

## 接口与行为

| 位置 | 本轮增量 |
|---|---|
| [ToolCallBatch.create](../agent_runtime/tool_batch.py) | 冻结注册表的嵌套 schema，检查整批调用身份/JSON 结构及可用原始 content，使用既有 validator 缓存各项参数错误 |
| [ToolBatchRunner.run](../agent_runtime/tool_batch_runtime.py) | 接收 native_content，将逐项拒绝作为已准备结果送入既有工具步骤；继续原序归并 |
| [AgentLoop](../agent_runtime/agent_loop.py) | native 回合传入原始 content；沿用 Observation、收据、取消和恢复流程 |
| [AnthropicCompatibleModelClient](../agent_runtime/providers/clients.py) | 保留原始 input、name、ID 和未过滤 content，非法 input 不再改写为 `{}` |

```text
完整 native 回合 → owner 创建批次
  → 整批身份、参数对象、原始 content 配对检查
  → 全部调用按冻结 schema 做纯预检并缓存错误
  → Scheduler 准备每项：错误项直接形成拒绝，合法项进入既有执行流程
  → owner 记录结果/Observation，按 ordinal 和 call ID 返回
```

| 情况 | 结果与执行边界 |
|---|---|
| schema 缺参、类型、未知字段或枚举错误 | 单项 `rejected / invalid_arguments`，合法兄弟调用继续；保留该项结果配对、拒绝收据和 Observation |
| 空/重复 ID、未知工具、非对象或不可序列化参数 | 整批 `tool_batch_protocol_error`，派发前退出，无工具执行 |
| 有原始 content，调用数量/次序、ID、名称或输入不一致；content 结构非法 | 同上，整批阻断；事件仅记录稳定错误码 |
| 原始 content 为 None 或空列表 | 沿用已有仅提供规范化调用的 adapter 契约；调用自身仍须通过结构和 schema 检查 |
| 明确提供 `{}`，且现有 schema 允许省略全部参数 | 接受；省略字段是否非法由现有 schema 决定 |
| provider 报输出截断，即使其中有看似完整的写调用 | 沿用整回合丢弃与有界恢复，零工具执行；本轮补充验证，未新增截断控制器 |
| schema 合格，但权限、路径、配额或当前状态不允许执行 | Executor 继续按现有门禁拒绝，预检不授予执行权限 |

预检使用参数副本并丢弃 validator 的规范化输出；原始参数、哈希与调用身份保持。既有紧凑 schema 的类型转换规则不变，例如可接受的 `start="2"` 留到 Executor 规范化。这里复用项目现有 schema 子集校验器，不新增完整 JSON Schema 引擎。原始/规范化调用使用排序 JSON 比对，保留布尔值与整数等差异。

参数拒绝带 `preflight=True`、`rejection_reason=invalid_args` 和结构化 `argument_preflight` 错误。它不创建执行 Action 或 Plan operation，不预约/消耗工具配额与 repair 调用预算；Scheduler 的既有读容量调度不变。拒绝收据和 Observation 只证明处理结果，不能作为已执行或副作用成功证明。运行时取消、耗尽、uncertain 等既有控制流仍可先终止后续项。

这是逐项批次执行：合法写入已经发生时，后面的拒绝不会回滚它。无批次事务、DAG、跨存储原子提交或 exactly-once 保证。原有 worker 清理、迟到结果拒绝、Plan journal 和后态对账仍负责恢复。

## 借鉴与技术取舍

| 参照 | 本轮采用 | 保留或后置 |
|---|---|---|
| Pi（R-05） | 将参数预检、执行和原序结果归并分开；调用生命周期保持可配对 | 不复制默认并行策略，不引入 TS 内核、新调度器或 hook 授权 |
| OpenCode（R-06） | 可信执行层仍掌握权限、路径和最终状态门禁 | 预检不审批、不替代 Executor/Gateway；已审计白名单保持 |
| 恢复边界对照（R-10） | 拒绝、完成事件、Observation 与执行事实分别解释 | 既有 journal/收据/后态对账保留，不由批次进度推断副作用成功 |
| Hermes（R-08/R-09） | 后续历史与经验能力依赖稳定执行边界 | 本轮不接入历史索引、自动提炼或 Skill 发布 |

来源见 [五维参照](AGENT_DESIGN_REFERENCES_2026-10-01.md)，本地决定见 [ADR-019](design-decisions.md)。参数错误格式、native 原始块核对及冻结快照是 FixLoop 本地实现，不声称参照产品提供相同协议或恢复保障。选择小增量接入已有步骤，避免同时重写两个协议循环。

## 验证记录

按唯一节点最新运行结果汇总：**119 passed、0 failed、0 skipped**，其中 [新增行为测试](../tests/test_native_batch_preflight.py) **20 个节点**。受控 provider 驱动真实文件 IO、worker 线程、子进程退出及 host pytest；未调用在线模型，未运行全量测试，未提交/推送或创建 PR。

| 报告 | 本批结果 | 验证与处理 |
|---|---|---|
| `baseline.xml` | 0 tests | 初始参数化 nodeid 不存在，无测试执行；保留报告 |
| `baseline-tests.xml` | 2 failed | 实现前复现缺参项走旧 Executor 路径及非法 input 被改为 `{}` |
| `first.xml` | 13 passed / 1 failed | 缺参、8 类原始协议损坏、4 类非法 provider input；截断断言的 call ID 与普通恢复提示词冲突 |
| `truncated.xml` | 1 passed | 改用唯一 call ID，精确复验截断零副作用和候选不进入下一请求 |
| `runtime.xml` | 50 passed / 1 failed | 批次/Scheduler、内核拆分、共享许可、恢复与真实进程退出；混合写测试误将可省略 content 当作 schema 错误 |
| `gates.xml` | 55 passed | 改用非法 append 类型精确复验合法写保留/非法写阻断；新增类型/未知字段用例，Executor/schema/provider 用量/text salvage，以及 XML/native L2 探索→owner 重读→单次修改→host pytest |

两个后续失败均先核对原因再修正测试：`discarded` 恰好出现在恢复提示中；WriteFileArgs 的 path/content 有默认值，省略 content 本来合法。后者改用真实非法布尔参数，未修改生产 schema。新增测试还覆盖嵌套 schema 冻结、原始参数不规范化、文本/thinking 块混合及明确空对象允许省略参数。

原始报告、[汇总脚本](../artifacts/batch-preflight-mvp-2026-10-02/summarize.py) 和 [summary.json](../artifacts/batch-preflight-mvp-2026-10-02/summary.json) 保留在 [artifacts](../artifacts/batch-preflight-mvp-2026-10-02/)。按 JUnit 开始时间选取节点最新结果，保留失败历史，不相加重复通过数。历史工具批次 T1–T9 记录保持原样。

本轮 6 个 Python 文件 Ruff check/format check 通过；相关已跟踪 diff check、73 个本地文档链接、新增文件与相关文档空白检查通过。两个批次模块的 AST 导入检查无 `src` 依赖；现有 Loop 的 L2 依赖未在本轮迁移。本验收不代表在线模型效果、全量回归或全仓 L1/L2 解耦完成。
