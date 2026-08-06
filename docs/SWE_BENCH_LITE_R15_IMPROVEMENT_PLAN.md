# SWE-bench Lite R15 改进方案

## 1. 文档目标

本文基于 `swebench_lite_dev_live_r14` 的运行产物和当前 FixLoop 实现，整理 R15 运行前应完成的改进项。目标不是针对单个 Case 调参，而是修复会系统性影响代码定位、工具执行、补丁产出、验证和评测可信度的问题。

R15 的首要目标是建立可信、可诊断的评测链路，然后再提升 Pass Rate。若基线仓库不干净、Gold 信息泄漏、空补丁被错误归类或超时无法真正中断，即使个别 Case 通过，也不能说明修复能力得到提升。

## 2. R14 结论

R14 共运行 5 个实例：Astropy、Django、Matplotlib、Pylint 和 SymPy。5 个实例均未产生可导出的有效补丁，最终都被归类为 `empty_model_patch`，因此 R14 不能作为 FixLoop 当前代码修复成功率的有效测量结果。

主要现象如下：

- 前四个实例进行了多轮读取和搜索，但没有进入有效写入阶段，随后耗尽 Step 或 Tool 配额。
- Astropy、Matplotlib、Pylint 的末次模型输出达到 4096 Token，最终答案为空，疑似被最大输出长度截断后误判为正常结束。
- SymPy 首次模型调用阻塞约 390 秒，虽然配置了 300 秒 Step Timeout，但 Runtime 没有在截止时间真正中断 Provider 请求。
- 多个实例出现 `stall_detected`，但告警没有改变后续控制流，模型仍可继续重复读取。
- Django 等工作区在运行前已经包含目标修改，快照也把这些旧改动记录成了“原始状态”，最终自然无法导出新 Patch。
- Patcher Prompt 中出现 EditLock 与“只允许修改文件”相互冲突的情况。
- `test_patch`、测试文件名和低置信度 Skill 对定位产生了污染，部分 Case 被引导到无关实现文件。
- R14 使用 `skip_verify=true`、`run_harness=false`，即使产出补丁也无法形成正式的 SWE-bench 解决率结论。

因此，R14 暴露的核心问题不是“模型不会修 Bug”这一单点问题，而是评测基线、定位、权限、循环控制、Provider 结束语义和验证环境共同造成的链路失效。

## 3. 改进原则

R15 改进遵循以下原则：

1. **先保证评测可信，再优化模型效果**：每个实例必须从指定 `base_commit` 的干净工作区开始。
2. **确定性骨架约束概率性决策**：状态流转、权限、超时、幂等和终态裁决由 Runtime 或 Orchestrator 控制。
3. **保持 Native Tool Calling 协议**：不把 Tool Call 降级成纯文本；只把多步循环的所有权从 Provider Client 上移到 Runtime。
4. **上下文压缩发生在安全边界**：完整保留未闭合的 `tool_use/tool_result` 配对，压缩已完成的旧读取记录。
5. **写入和验证拥有保留预算**：读取行为不能耗尽全部 Step 和 Tool 配额。
6. **所有失败都应可归因**：避免把超时、定位错误、基线污染和输出截断统一记录为 `parse_fail` 或 `empty_model_patch`。

## 4. P0：R15 前必须完成

### 4.1 隔离并校验评测基线

每个 SWE-bench 实例使用一次性的独立 Worktree 或临时仓库，并从 Case 指定的 `base_commit` 创建。运行前执行 Preflight：

- 当前 `HEAD` 必须等于 `base_commit`。
- Git 语义 Diff 必须为空。
- 不允许继承上一次运行的代码修改、未跟踪文件或测试产物。
- 记录换行符配置，避免 Windows CRLF 造成全文件伪 Diff。
- Preflight 不通过时立即标记为 `baseline_dirty`，禁止启动 Agent。

`--skip-clone` 只能表示不重复下载仓库，不能表示复用未经重置和清理的工作区。补丁导出应比较 `base_commit → 最终工作区`，而不是比较运行前复制的、可能已经污染的快照。

### 4.2 严格隔离 Gold 信息

正式评测模式不得向 Agent 暴露以下信息：

- Gold Patch；
- Gold Test Patch；
- `FAIL_TO_PASS` 和 `PASS_TO_PASS` 的答案性内容；
- 从 Gold Patch 反推的目标文件或符号。

这些信息只能由独立 Verifier 或官方 Harness 使用。允许保留开发辅助模式，但必须在 Manifest 和 Report 中明确标记为 `assisted`，不得与严格评测结果混合统计。

### 4.3 统一 EditLock 的权威来源

当前 Prompt 中的“允许修改文件”和 Executor 实际执行的 EditLock 可能来自不同字段，导致模型被要求修改某文件，同时又被告知只能修改另一文件。

改进方式：

- 由 Orchestrator 生成唯一的 Effective EditLock。
- Prompt、ToolGateway、Patch Preview 和最终 Apply 均读取同一份 Effective EditLock。
- `RepairPlan.suspect_files` 只表达定位结果，不直接承担权限语义。
- Prompt 构建后增加一致性断言；若展示范围与执行范围不同，任务在模型调用前失败。

### 4.4 修复定位污染与 F2P 映射

不能通过扫描 Gold `test_patch` 来推断实现文件。严格模式下，定位只使用用户 Issue、公开 Traceback、当前仓库和允许的确定性索引。

F2P 或测试文件到实现文件的映射应按以下顺序进行：

1. Traceback 中的实现帧和符号；
2. 测试导入、调用目标和断言相关符号；
3. 同名模块的显式映射；
4. 文本搜索、符号索引和 AST/调用关系；
5. 低置信度时交给 Retriever 扩展检索。

禁止在映射失败后选择目录中按字母排序的前几个 Python 文件作为候选。这种 Fallback 应替换为 `localization_low_confidence`，并触发完整检索链路。

### 4.5 Patcher-primary 动态升级

`patcher_primary` 不应无条件跳过 Localizer 和 Retriever。出现以下任一条件时，应动态升级到完整链路：

- 没有可信实现文件候选；
- 候选只有测试文件；
- Traceback 与当前源码无法对齐；
- F2P 映射失败或置信度低；
- Patcher 请求的目标不在 EditLock 中；
- 连续读取后仍无法形成修改假设；
- Patcher 明确返回 `needs_more_context`。

升级路径应记录到 Trace 和 Report，便于评估动态裁剪节省的成本及其对 Pass Rate 的影响。

### 4.6 将 Native Tool Loop 控制权上移到 Runtime

当前 Provider Client 内部持有完整多步工具循环，Runtime 只在开始时构建一次 Context。这会使旧 `read_file` 结果、重复告警和失败信息持续堆积，而 ContextManager 无法在每一步重新预算和压缩。

建议新增“单步原生工具调用”接口，例如 `complete_with_tools_once`：

1. Runtime 构建本轮 Context；
2. Provider 返回 Native `tool_use` 或文本结束结果；
3. Runtime 统一解析 Tool Call；
4. ToolGateway 和 ToolExecutor 执行；
5. Runtime 记录 Observation、Checkpoint 和 Trace；
6. ContextManager 在下一轮前去重、压缩和重建 Context。

该方案不放弃原生 Tool 协议。发送给 Provider 的仍然是原生 Tool Schema，返回的仍然是原生 `tool_use`，工具结果仍以原生 `tool_result` 回填。变化只在于 Runtime 获得逐步控制、预算、取消、审计和恢复能力。

压缩时必须遵守协议安全边界：

- 未完成的 `tool_use/tool_result` 对不得拆开；
- 最近 2～3 组完整交互保留原文；
- 更早且已闭合的读取结果替换为结构化摘要或外部引用；
- 写入、验证、权限拒绝和当前错误保留更高优先级。

### 4.7 修复 Provider 结束原因处理

ModelClient 必须记录并向 Runtime 传递 Provider 原始结束原因，至少区分：

- 正常文本结束；
- `tool_use/tool_calls`；
- 达到最大输出长度；
- 内容审核拦截；
- 空响应；
- 网络、限流和 Provider 错误。

达到 `max_tokens` 或返回空内容时，不能被解释为 Final Answer。可执行一次有界续写或结构化补全；仍失败则记录 `model_output_truncated` 或 `empty_model_output`。同时应根据剩余 Context、预期 Patch 大小和任务阶段动态设置最大输出预算，避免固定 4096 Token 截断大补丁或长推理结果。

### 4.8 让 Timeout 真正中断阻塞调用

Step Timeout 不能只在调用返回后统计超时。Runtime 应把剩余 Deadline 传递到 Provider HTTP Client，并保证：

- 单次请求 Timeout 不超过 Step 剩余时间；
- Provider 内部重试共享同一个 Deadline；
- Cancellation Token 能中断或放弃等待中的请求；
- 超时后迟到的 Tool Call 或模型结果不再进入执行链路；
- Trace 同时记录配置 Timeout、实际耗时和中断位置。

SymPy Case 应作为 R15 的超时回归用例，确认 300 秒限制能在合理误差范围内生效。

## 5. P1：直接提升补丁产出率

### 5.1 建立读取去重和证据账本

对 `read_file`、搜索和符号查询增加去重机制：

- 以文件版本、路径、行区间、查询参数和内容 Hash 识别重复读取；
- 合并重叠行区间；
- 写文件后使相关摘要和读取缓存失效；
- 最近读取保留原文，历史读取转为证据账本。

证据账本至少保存路径、行区间、符号、结论、内容 Hash、来源和有效性。模型可以按需重新展开原文，而不是让所有旧源码永久堆在 Messages 中。

### 5.2 强化 No-progress 控制流

`stall_detected` 必须改变执行路径，而不只是追加警告文本：

- 连续 2 次无进展：返回结构化告警，要求给出当前假设和缺失证据；
- 连续 3 次无进展：禁止重复读取，要求写 Patch、扩大检索或重新规划；
- 连续 4～5 次无进展：终止当前策略，升级 Retriever、切换 Fallback 或返回可诊断失败。

No-progress 可综合以下信号判断：

- 重复 Tool Call；
- 重复读取相同内容；
- Suspect、Hypothesis、Candidate Patch 和 Task State 均无变化；
- 多轮只增加自然语言分析，没有新证据或新动作；
- Tool Result 的内容 Hash 高度重复。

不要把长篇重复告警附加到每次 Tool Result 中，只注入简短结构化状态，例如 `no_progress_count`、`reason` 和 `required_next_action`。

### 5.3 拆分工具配额并预留写入能力

将单一工具总配额拆成：

- Read/Search Budget；
- Write/Patch Budget；
- Verify Budget；
- Recovery Budget。

读取预算耗尽后，仍应保留至少一次 Patch 和一次验证机会。对连续重复读取单独限额，避免探索行为消耗全部执行能力。工具配额应同时按 Run、角色、工具和 ToolGroup 记录，便于定位瓶颈。

### 5.4 对齐 Skill、角色白名单与真实工具

最终暴露给模型的工具集合应取以下交集：

`Skill Suggested Tools ∩ Role Whitelist ∩ Registry Healthy Tools ∩ Current Permission`

同时建立工具别名归一化，避免 Skill 推荐 `search`、`patch_file`，但实际 Registry 只有其他名称。低置信度 Skill 不应注入完整执行策略；只有达到阈值且通过 Negative Trigger 检查时才加载正文，否则只保留弱提示或使用通用策略。

### 5.5 建立 Patcher 结构化终态契约

Patcher 不应通过空文本表达失败。建议统一为以下终态：

- `patch_produced`；
- `needs_more_context`；
- `already_fixed_with_baseline_evidence`；
- `cannot_patch`；
- `model_output_invalid`。

`already_fixed` 必须基于干净 `base_commit` 和明确证据验证，不能只凭模型判断。若正常 Loop 未产出补丁，可增加一次受限的 Patch-only Completion：只提供已选目标、必要代码、失败证据和 `apply_patch` 能力，不允许无限重试。

### 5.6 提升验证反馈质量

Verifier 反馈至少包括：

- 执行命令和测试 Node ID；
- Expected、Actual 和关键断言；
- 最短相关 Traceback；
- Patch Hash 和影响文件；
- 基线失败还是补丁引入失败；
- 代码失败还是环境失败；
- 建议下一轮关注的文件、符号或行为。

反馈只投影给需要它的 Patcher，避免将大段测试日志污染所有角色 Context。下一轮应在 Trace 中记录实际采用了哪些反馈，便于判断 Retry 是否真正改进，而不是随机再生成一次。

## 6. P1：验证与 SWE-bench Harness

### 6.1 分层验证

建议采用以下验证顺序：

1. Patch 格式、路径和 EditLock 校验；
2. 语法、导入和轻量静态检查；
3. 与修改范围相关的目标测试；
4. 必要的回归测试；
5. 官方 SWE-bench Harness。

Host 环境只适合执行可信的轻量静态检查。正式动态测试应运行在 Docker、WSL 或 SWE-bench 官方兼容环境中，避免 Windows 依赖和收集错误被误判为代码失败。

### 6.2 区分验证结果

最终状态至少区分：

- `patch_generated_unverified`；
- `target_tests_passed`；
- `regression_failed`；
- `verification_environment_failed`；
- `official_harness_resolved`；
- `official_harness_unresolved`。

`skip_verify` 仅用于链路 Smoke Test，不得计入正式修复成功率。

### 6.3 缓存安全

构建缓存、依赖缓存和测试缓存必须包含仓库版本、Patch Hash、运行环境、依赖锁文件和测试选择等关键信息。正式验证至少应提供一次禁用结果缓存的复核路径，防止旧缓存造成假通过。

## 7. P2：可观测性和报告改进

### 7.1 细化失败分类

失败归因支持一个 Primary Cause 和多个 Contributing Causes，至少覆盖：

- `baseline_dirty`；
- `gold_data_leakage`；
- `localization_miss`；
- `edit_scope_conflict`；
- `no_write_progress`；
- `tool_quota_exhausted`；
- `tool_permission_mismatch`；
- `model_output_truncated`；
- `empty_model_output`；
- `step_timeout`；
- `verification_environment_failed`；
- `empty_patch`。

避免把所有失败统一落为 `parse_fail`，否则无法指导下一轮工程改进。

### 7.2 统一终态收尾

Finalizer 应使用幂等键或 Compare-and-Set 保证只执行一次，并同步完成：

- Task State 终态写入；
- Stop Reason 固化；
- Trace 结束事件；
- Report 生成；
- Metrics 结算；
- 临时资源清理。

如果出现冲突终态，按确定性优先级裁决，并记录被抑制的候选终态。不能出现 Repair 已失败但主 Task State 仍为 `running` 的情况。

### 7.3 完善 Manifest

Manifest 应记录：

- 实际 Provider 和解析后的模型名；
- `base_commit`、运行时 `HEAD` 和基线清洁状态；
- `skip_clone`、`skip_verify`、`run_harness`；
- 严格模式或辅助模式；
- Prompt、Skill、Tool Schema 和配置指纹；
- 换行符及运行环境；
- Context Window、最大输出长度和 Timeout 配置。

### 7.4 区分批次完成与任务成功

`adapter_report.ok=true` 只能表示批处理程序完成，不应被理解为修复成功。报告应分别提供：

- Run Completed Rate；
- Non-empty Patch Rate；
- Verification Pass Rate；
- Official Resolved Rate；
- Baseline Dirty Rate；
- Localization Escalation Rate；
- No-progress Termination Rate。

## 8. 推荐实施顺序

按以下顺序实施，避免后续优化建立在不可信评测基础上：

1. 基线隔离与 Preflight；
2. Gold 数据严格隔离；
3. 移除 `test_patch` 定位泄漏并修复 F2P Fallback；
4. 统一 Prompt 与 Executor 的 EditLock；
5. Patcher-primary 动态升级；
6. Runtime 接管单步 Native Tool Loop，并加入安全压缩；
7. 读取去重、No-progress 控制和工具配额拆分；
8. Tool/Skill/权限集合对齐；
9. Provider Finish Reason 和真实 Timeout；
10. Docker/官方 Harness 验证与结构化 Feedback；
11. Task State、Trace、Manifest 和 Report 收尾修复。

## 9. R15 分阶段运行方案

### 阶段一：单 Case 工程门禁

按以下顺序运行，用每个 Case 验证一个关键能力：

1. **Django**：运行前必须保留旧 Regex；Agent 必须产出非空 Patch，不能继承 R14 修改。
2. **Matplotlib**：必须定位到正确 Backend 实现，不能使用“目录前几个文件”Fallback。
3. **Astropy**：Prompt 中只允许出现一份权威 EditLock，不得同时限制到测试文件和实现文件。
4. **Pylint**：运行前不得包含历史修复，补丁必须从指定 Base Commit 新生成。
5. **SymPy**：300 秒 Step Timeout 必须在合理误差内真正终止阻塞请求。

### 阶段二：固定 Dev5 回归

保持模型、Prompt、预算和环境不变，运行固定 5 个实例，重点观察：

- 非空 Patch 率；
- 首次写入前的平均读取次数；
- No-progress 触发后的路径变化；
- 定位升级率；
- Tool Permission Rejection；
- 输出截断率；
- Target Test 和 Harness 结果。

### 阶段三：正式 Harness

关闭 `skip_verify`，启用官方 SWE-bench Harness。只有 Harness 判定 resolved 的实例才计入正式解决率。至少保留一组与 R14 配置相近的对照实验，以区分工程链路修复与模型随机波动。

## 10. R15 启动门槛

运行正式 R15 前必须满足：

- [ ] 5/5 实例通过基线清洁检查；
- [ ] Agent Context 不包含 Gold Patch、Gold Test Patch 或答案性元数据；
- [ ] Prompt 展示权限与 Executor EditLock 零冲突；
- [ ] 低置信度专用 Skill 不会强注入完整策略；
- [ ] Patcher-primary 在低置信度定位下能够升级检索；
- [ ] 连续无进展读取不超过设定阈值；
- [ ] Read Budget 耗尽后仍保留 Write 和 Verify Budget；
- [ ] 每次模型调用都记录 Provider Finish Reason；
- [ ] `max_tokens` 与空响应不会被识别为正常 Final Answer；
- [ ] Step Timeout 能中断或丢弃迟到的 Provider 结果；
- [ ] 所有 Task State 都收敛到终态；
- [ ] 只有非空且通过格式与权限校验的 Patch 才进入 Verifier；
- [ ] 正式结果由官方 Harness 判定。

## 11. R15 核心验收指标

R15 除 Pass Rate 外，建议至少记录以下指标：

- Baseline Clean Rate：目标 100%；
- Non-empty Patch Rate；
- Patch Apply Success Rate；
- Target Test Pass Rate；
- Official Harness Resolved Rate；
- Localization Precision 与 Escalation Rate；
- 首次有效写入前的 Tool Call 数；
- 重复读取率和 No-progress Rate；
- Tool Rejection Rate，按角色、工具和闸口分桶；
- Output Truncation、Empty Output 和 Timeout Rate；
- 每个 Case 的 Token、端到端延迟和 Provider 调用次数；
- Task State/Trace/Report 一致性通过率。

## 12. 预期收益

完成 P0 后，R15 将首先获得可信的基线、真实的超时和可解释的失败原因；完成 P1 后，Agent 能减少重复读取，在预算耗尽前进入 Patch 和 Verify，并在定位不足时主动升级检索；完成 P2 后，可以从 Trace 和 Report 中区分模型能力不足、定位错误、权限冲突、环境失败和编排缺陷。

这些改动不保证所有 SWE-bench Case 立即通过，但能把当前“运行了很久却没有补丁”的不可诊断状态，转化为可测量、可回归、可持续优化的代码修复闭环。
