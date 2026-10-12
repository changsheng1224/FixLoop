# S2：消息来源与版本化状态

日期：2026-10-12。分支：`M1/D2/message-provenance-state-s2`，基于 S1 的 `c4d5ad8`。

## 实现范围

原始 Issue → 可信宿主登记消息 → 显式结构化批次 → owner 校验 → journal 原子提交 → 当前状态投影 → 下一轮请求/委派。

本阶段没有自然语言自动提取、语义冲突裁决或缓存修改；没有新增模型调用、工具权限、聊天 UI 或消息队列。已存在的原始目标、Plan、证据和执行收据仍由各自原有机制管理。

## 模块与契约

| 模块 | 职责 |
| --- | --- |
| `agent_runtime/plan_runtime/task_messages.py` | 不可改写的消息 ID、task/run/workspace/session 身份、来源种类、完整正文和 checksum；校验原文引用 |
| `agent_runtime/plan_runtime/constraint_records.py` | constraint_id/revision/status/source_refs 版本链；新增、替代、撤销；活动字符串投影 |
| `agent_runtime/plan_runtime/context_updates.py` | 宿主消息 API、一次性初始化、owner 批次校验、CAS、持久化和重放 |
| `agent_runtime/plan_runtime/decisions.py` | 扩展既有节点决策链，区分 user/technical 来源，新增撤销；保留技术证据与 needs_review |
| `agent_runtime/plan_runtime/journal.py` | append_many 将批次回执与完整 long_task_state 写入同一个 SQLite 事务和校验链 |
| `agent_runtime/plan_runtime/session.py` | 恢复/请求构建时验证来源；未确认提交阻止 checkpoint、Plan 推进和工具派发 |
| `src/repair/plan_binding.py` | 在规划前登记原始 Issue，结构化初始约束独立标为 runtime 来源 |
| `src/collaboration/exploration_projection.py` / `exploration_runtime.py` | 委派携带活动节点决策和 context_revision；收集时使旧上下文结果失效 |

来源身份由受信任宿主调用 `register_user_message` / `register_model_message` 确定；这些 API 不在模型工具目录中。批次没有可自行赋值的 source_kind 或权限字段。引用格式为 message_id、message_checksum、start、end、quote；位置按 Python 字符串索引（Unicode code point，左闭右开），全文和引用均不截断。checksum 用于一致性校验，身份信任来自宿主边界，并非数字签名。

初始 `RepairState.hard_constraints` 是显式宿主配置，单独登记为 runtime 消息，不伪造为 Issue 原文。后续约束仅接受 user 来源；模型来源不能提交硬约束。运行时核验引用存在及角色正确，不推断引用与条款在语义上等价。

原始目标只初始化一次。相同初始化请求可重放，改变原始目标或初始配置会被拒绝。后续约束变化必须通过显式批次；重新初始化不会覆盖已经更新的活动约束。

`constraint_records` 是约束真值。序列化和既有消费者继续使用 `hard_constraints` 字符串列表，但该列表由最新活动记录生成，并在恢复、提交和消费时与版本链核对；直接修改列表不能通过校验。旧记录保持不变，superseded 只在历史视图派生；revoked 是持久化的新版本，重新启用需新建 ID。

用户决策使用独立 source_refs，允许不提供代码 evidence_refs。技术决策继续要求非空、有效且 checksum 固定的技术证据；通过批次提交时还要求 model 消息来源。既有宿主 `record_decision` 接口仍可提交有证据的技术决策，其 source 标签不会赋予用户身份。两类来源不能混写或跨类别替代。节点决策保持既有作用域和执行阶段限制，不自动绑定节点，也不允许改写已完成节点的选择。

## 显式更新示例

```python
session.configure_long_task(issue, hard_constraints=["不得修改现有测试"])
session.register_user_message("user-turn-2", "新增要求：保留公共 API")
receipt = session.submit_context_updates(
    "user-turn-2:constraints:1",  # 重试必须保留同一个 batch_id 和请求
    expected_state_revision=session.long_task_state.state_revision,
    expected_plan_version=session.plan.plan_version if session.plan else 0,
    updates=[{
        "kind": "constraint",
        "action": "add",
        "statement": "保留公共 API",
        "source_refs": [session.source_reference("user-turn-2", 5, 13)],
    }],
)
constraint_id = receipt["records"][0]["constraint_id"]
```

替代/撤销需要目标 ID 和 expected_revision。决策还需要 node_id；技术决策需要 evidence_refs。宿主负责给出明确条款及引用区间，没有自动解析上述自然语言的步骤。

## 提交、恢复与委派

- 注册同一 message_id/正文/角色不重复写入；正文或角色不同则冲突。一个消息可以在不同显式批次中引用不同条款。
- 相同 batch_id 和完整请求返回原回执。改变请求、来源、预期状态版本或 Plan 版本则冲突；重放不新增记录，也不把最新状态回滚到旧回执的状态。
- 所有条目先在隔离状态副本上校验。任一来源、字段、目标 revision、节点或证据失败，整个批次不发布。一次成功批次只推进一次 state_revision/context_revision。
- 回执包含输入状态版本、Plan ID/version、输出版本、结果记录及状态 checksum，与状态快照在同一事务中提交；没有跨库事务或后置消费游标。
- 事务前进程退出：无活动状态发布，可以用原请求重试。事务后进程退出：恢复加载完整状态，原批次重放返回原回执。
- 提交成功但确认/内存发布中断：当前 session 标记结果待对账，阻止旧 checkpoint、Plan 推进和新工具派发；原批次重放可恢复。事务异常且找不到提交记录时，重新打开 session 根据 journal 对账。
- 未停止或 uncertain 的操作阻止新更新。已确认 patch/test 收据、attempt 和 Plan 节点状态不会被上下文更新改写，不重新执行已确认操作。
- context_revision 只在初始配置、显式批次或显式决策提交时变化。节点进度、证据追加、审计事实使用原 state_revision；它们不会单凭版本推进使正常 Explorer 结果失效。约束或决策变化则使旧委派结果失效，即使目标和约束字符串相同。

消息正文保存在同一个本地 journal，未新增删除/保留期限机制。本阶段保证新登记来源，不从旧无来源记录或摘要补造用户身份。

## 验证记录

只运行相关测试，未运行全量测试、付费模型或发布级验证。

- 原有上下文、决策、required context、委派一致性、L2 repair/replan：首次 114 passed、1 failed。失败是 task-projection 单测的 SimpleNamespace 缺少无关 memory access 字段；隔离该单测的 knowledge 依赖后，失败 nodeid 通过。
- 新增 `test_context_updates.py`：24 个用例均通过，包括来源身份/正文绑定、引用伪造、CAS、批次回滚、用户/技术决策、替代撤销、owner 失效、真实子进程退出前后恢复、确认丢失门禁、在途操作、patch/test 收据保留。
- context assembly / 独立 Explorer 补充回归：65 passed、1 个独立 Explorer deadline 用例失败。该用例使用 150ms 的总截止时间，诊断发现可能在受控模型入口前超时；定点复现，并使用 S1 HEAD 原版 ExplorationRuntime 再次复现。其 standalone 路径没有 PlanSession，不经过 S2 新逻辑；未修改该超时机制或放宽断言。
- Plan runtime / crash windows：43 个用例通过；最终决策投影及在途门禁定点复验也通过。
- 合计 248 个不同相关用例中，247 个已通过，1 个上述既有 deadline 用例未通过。已通过且未受后续修改影响的结果沿用，未重复整批运行。
- 修改文件 Ruff check、format check 和 git diff whitespace 检查通过。

## 面试口述

“我们把模型看到的文本和可提交的任务状态分开。用户身份由宿主登记，结构化条款必须引用原消息；约束维护版本链，技术决策还绑定当前证据。owner 用状态版本和 Plan 版本做比较校验，把整个批次与快照一次写入 journal。恢复和重复提交按 batch_id 对账；模型请求和委派每次读取当前投影，旧结果因上下文版本变化失效，但已经执行成功的补丁与测试收据保持有效。MVP 的语义条款由宿主显式提供，没有承诺自动理解全部自然语言要求。”
