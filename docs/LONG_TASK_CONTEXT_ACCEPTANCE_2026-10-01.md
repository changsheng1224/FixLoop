# 长任务状态与上下文 MVP：实现与验收

开始日期：2026-10-01；验收日期：2026-10-02。本轮落实讨论后确认的推荐范围：统一权威状态投影、必需预算门禁、恢复后重建当前上下文。完整旧规格仍为部分实现，决策版本和自动提炼不在本轮范围。

## 交付范围

1. `PlanSession.build_required_context()` 从当前已校验 Plan 与已持久化 LongTaskState 生成只读投影：完整原始目标、显式约束、当前节点、完成条件、依赖状态、必要证据引用和身份/校验和。运行节点取当前 attempt；恢复后从当前 Plan 选择可执行节点，不沿用历史 current_node 指针。多节点无法确定时明确阻断。
2. ContextManager 只填充一次 state。带 Plan 的请求先核算完整执行规则、角色规则、工具协议、目标、约束、当前节点和本轮请求；可选工作区、记忆、源码及历史只使用剩余预算。必需项不使用 200-token state 限额、不裁剪、不以摘要替代。通用调用示例不常驻 Plan 请求；角色规则仍完整保留。默认预算保持原值。
3. XML/native 共用语义门禁。native 工具 schema 通过 provider 协议提供并预留预算，不重复塞入文本工具说明；最终核算工具定义、消息和系统文本，超预算时先移除完整可选 native tail。XML 预留分隔符并核算组装后文本。输出截断恢复保留原目标、约束和节点。发送前再次验证快照，包括回调可能造成的变化。
4. 当前节点所需输入不可验证时返回 `needs_retrieval`。无关旧证据不阻断新的探索。已有确认 patch/test 收据作为后态证据，不为刷新旧输入而重复修改。节点 uncertain 或 attempt 中仍有未确认结果时返回 `action_uncertain`。
5. context manifest 和 checkpoint 保留必需状态引用、逐 section 内容 hash、必需项 token 数与协议预留。恢复先沿用已有 owner/journal 对账，再重建当前投影；缓存摘要和 Todo 不成为权威状态。合法 Plan 前进不因为旧 current_node/revision 而被误拒；必要证据失效仍要求显式重取。
6. `RepairState.hard_constraints` 支持显式传入及序列化，选定 L2 初始化路径写入已有 LongTaskState。原请求保留全文。不从自然语言或摘要自动推断并落盘约束、决策。
7. 公开入口取得 owner 后即续租，覆盖慢初始化窗口，再交接给已有 PlanBinding 心跳。未对账的 owner 仍不得派发工具；不扩大租期或放宽 generation/status 校验。此补充来自公开入口回归暴露的边界。

稳定阻断原因：`context_required_over_budget`、`state_mismatch`、`task_request_missing`、`plan_context_node_required`、`needs_retrieval`、`action_uncertain`。AgentLoop 记录 `context_blocked` 事件与终态，受影响的请求不发送给模型。

## 借鉴与技术取舍

沿用 [五维参照 R-02/R-03/R-10](AGENT_DESIGN_REFERENCES_2026-10-01.md) 的已核对来源：

| 参照 | 本轮采用 | 技术取舍 |
|---|---|---|
| OpenCode 上下文预留、工具输出裁剪 | 先给完整必需内容留预算，其他项受剩余额度约束 | 不复制上游 token 常量；不自动扩大预算。完整内容过长时明确阻断。 |
| Pi transformContext / convertToLlm 边界 | XML/native 共享 Plan/任务投影校验，协议编码后再次核算 | 复用 Python ContextManager 和 AgentLoop；未进行整轮内核拆分。 |
| Pi 历史摘要与保留边界 | 摘要只承担历史提示，完整约束不依赖摘要 | 保留 L0–L5，不增加摘要模型、自动决策抽取器或新的上下文数据库。 |
| 对话继续与执行恢复边界 | 从当前 journal/Plan 重建上下文，确认收据决定已发生操作 | 不把旧摘要、Todo 或失效的写前源码当成重放依据；不承诺 exactly-once。 |
| Hermes 专项补充 | 本轮继续后置历史检索与经验候选 | 不增加跨任务检索、自动发布 Skill 或经验激活，控制 MVP 工作量。 |

首期保留原请求全文；未实现“超长原文保存引用 + 受信任摘要投影”。因此长目标、长角色规则或长约束超过预算时会阻断。这是明确失败契约，不是自动提炼遗漏内容后的继续执行。

现有 `record_decision()` 仍用于显式审计记录。本轮不增加 active/superseded、supersedes 链、决策证据 needs_review，也不声称所有历史决策都已投影为当前有效事实。

必要证据先校验并传递引用；源码正文继续由现有受控 Observation/探索路径按需消费，不把完整 blob 无条件钉入 prompt。本轮未新增自动工具重取或重规划触发。

## 验证与证据

Windows / Python 3.13.9，Fake provider 受控输出；真实文件读取、patch、host pytest 和进程崩溃恢复。未安装依赖、未调用付费模型、未运行全量测试或发布级验证。

最终相关结果：**302 passed、0 failed、0 skipped**。见 [summary.json](../artifacts/long-task-context-mvp-2026-10-01/summary.json)，按最新结果对测试节点去重，不累加重叠批次。可运行同目录 `summarize.py` 重新核算；JUnit XML 保留初始失败和精确复验记录。相关 13 个代码/测试/统计文件 Ruff lint 与 format 检查通过，Git diff whitespace 检查通过。

| 验证场景 | 证据 |
|---|---|
| 错误封印摘要、长约束、原目标和当前节点进入实际 XML/native 请求 | `tests/test_required_long_task_context.py`；required / fixes / core / final XML |
| 必需项超预算、缺目标、角色规则过长、状态/Plan 校验失败：零请求 | 同文件预算/状态/角色/目标场景；final XML |
| 可选组装期间和 pre_model 回调篡改状态 | 同文件 final_state / pre_model 场景；resume / final XML |
| 只验证当前输入、无关旧证据不阻断探索、确认后态不要求新鲜写前源码 | 同文件 exploration / missing_required / confirmed_patch / unconfirmed_operation 场景 |
| checkpoint 保留引用、当前 Plan 合法前进、缓存无法覆盖、缺 owner Plan 或输入过期拒绝 | 同文件 resume_rebuilds / resume_requires 场景；resume / final XML |
| 上下文压力→实际修改→关闭/reopen→实际 host pytest：一份写操作和一次验证 attempt，测试文件原样 | 同文件 l2_context_pressure 场景；resume / final XML |
| native 输出截断恢复仍完整保留中文目标/约束和节点，不保留截断输出 | 同文件 native_output_recovery 场景；recovery / final XML |
| 公开 repair、真正子进程崩溃后的恢复；Plan、旧 checkpoint、批次续接和检索消费 | regression / public-fixed / entry-fixed XML |
| 慢初始化续租、失败清理、取消后恢复不发请求、补丁后取消清理与回滚 | entry-fixed / entry-test-fixed / entry-cancel XML |

初批七个失败中，六个是测试读取终态字段位置错误；一项定位到 native 重复工具说明造成预算浪费。后续回归两项失败分别涉及 lease 过期与旧默认预算不足。运行期间发生时钟跳变，但精确复验还定位到入口慢初始化没有持续续租；补齐该窗口心跳后，公开修复用例通过。预算问题通过去除通用调用示例解决，保留所有角色规则。新增短租期测试曾误把 reconciling 当成可派发状态，已修正为验证续租且禁止派发。未放宽 lease 校验或增加默认预算。

## 限制与后续

- 强门禁作用于已绑定 PlanSession 的选定运行路径。普通 L1 Todo 请求仍沿用既有策略；前置轻量规划调用、全部 L1/L2 入口和新后端没有整体迁移。
- 不实现决策版本、自动约束/决策提炼、超长任务语义摘要、统一历史检索或经验激活。旧 spec 的 C1–C9 不能整体标为通过。
- Plan evidence 继续使用既有 ledger 校验，可能进行工作区快照扫描；未新增性能缓存或目录监听。不宣称 token、时延或线上修复率提升。
- 消费复验不是文件系统原子快照；检查后并发外部改写仍由现有工具/Plan 执行门禁处理。恢复仍要求当前 owner 和已有停止/收据证明。
- Git 元数据在本会话权限中只读。实现和测试证据保留在工作区，未创建分支、commit、push 或 PR，未覆盖其他已有修改。
