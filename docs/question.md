# Agent 面试题库（14 类）

> 统一主题分类（不区分通用概念与项目实现）；共 **14 类**，≤20 类上限。

> **总题量：670 题**

## 目录

1. **Agent 范式、Harness 与运行时**（135 题）
2. **系统架构与 Multi-Agent 设计**（54 题）
3. **Context、Prompt 与缓存工程**（14 题）
4. **结构化输出与解析**（13 题）
5. **记忆工程**（64 题）
6. **工具、Skill 与协议生态**（78 题）
7. **RAG 与知识检索**（25 题）
8. **代码修复流水线与自愈闭环**（116 题）
9. **运行时控制、Cancel 与 Checkpoint**（49 题）
10. **安全、沙箱与 Human-in-the-Loop**（24 题）
11. **评测、幻觉与质量保障**（32 题）
12. **可观测、Trace 与运维韧性**（18 题）
13. **性能、流式与 LLM 网关**（31 题）
14. **系统设计、会话与部署**（17 题）

---

- Agent 范式、Harness 与运行时
    1. 怎么理解 Agent Loop（感知 → 推理 → 工具 → Observation → 再推理）
    2. Agent Hook 是什么？一般挂在哪些生命周期点
    3. 什么是 Harness Engineering？它和 Agent Framework 是一回事吗
    4. Harness 一般包含哪些核心模块（运行时、工具、上下文、观测、安全等）
    5. 多任务分解 Planning 在工程里怎么落地
    6. Workflow 具体怎么落地使用？节点失败时重试、跳过、回滚怎么选
    7. 反思自纠错有没有价值？会不会越纠越错，怎么加刹车
    8. 为什么 Critic / Reviewer 不能无限循环
    9. few-shot 为什么有效？在 Agent 里怎么用才不过拟合
    10. Chain-of-Thought 为什么能提升推理？和 ReAct 怎么配合
    11. 大模型和规则引擎怎么分工？哪些环节必须用规则
    12. 小模型做路由、大模型做执行，怎么设计
    13. 大模型做路由和小模型做路由，准确率与成本如何取舍
    14. 模型路由错了，怎么快速回退
    15. 为什么很多 Agent 产品同时支持 CLI 和 API
    16. 系统变慢时，Agent 链路从哪里开始查
    17. 用 2 分钟介绍你负责从 0 到 1 的 Agent 模块，边界在哪
    18. 各子 Agent 的核心职责与分工边界如何界定（定位 / 检索 / 改码 / 验证等）
    19. 多 Agent 震荡时，怎么加终止条件（轮次上限、无进展检测、人工介入）
    20. 一句话里包含多个意图时，先拆意图还是先路由到 Agent
    21. 超长 user 粘贴（如 CI log），应该在入口层还是编排层做预处理
    22. 一个 utterance 多标签意图（Multi-label）时，置信度阈值和执行顺序怎么定
    23. 上下文相关意图（同一句话在不同会话状态下路由不同），规则还是模型更稳
    24. 意图置信度低于阈值时，澄清问句、默认意图、转人工，三条路怎么排优先级
    25. 并行调度多个意图时，共享 Context 还是各意图独立 Context，怎么防互相污染
    26. 哪些流程决策必须确定性实现，不能交给 LLM 临场判断？
    27. 各 Agent 私有的对话 history 与 memory，为何不能跨角色共享指针？
    28. `phase`（当前阶段）与 `status`（终态）为何要拆成两个维度？
    29. 阶段枚举通常包含哪些值？「进行中」与「已结束」如何区分？
    30. 终态 `fixed`、`patched`、`exhausted`、`failed` 各自代表什么业务含义？
    31. 重试计数与最大重试次数由谁维护？它们控制的是哪一段循环？
    32. 同一 key 同来源多次写入，覆盖策略是什么？
    33. 同一 key 不同来源写入，为何拒绝静默覆盖而记录冲突？
    34. 冲突仲裁策略（偏好定位结果 / 合并 / 最新写入）各适用什么场景？
    35. 声明式工具策略表如何按角色绑定可见工具子集？
    36. 分角色独立配额（如改码 write 与定位 read 分开计数）解决什么耗尽问题？
    37. 检索阶段耗尽总调用配额，是否应阻断后续改码？由谁决策？
    38. feedback 里为什么要包含「上一轮补丁 diff 或改动摘要」？
    39. 终态 `fixed` 的判定条件是什么？
    40. 终态 `patched` 与 `fixed` 的差异是什么？何时会出现 `patched`？
    41. 终态 `exhausted` 表示什么？与 `failed` 如何区分？
    42. 终态 `regression` 的业务含义是什么？如何检测「修好了目标测试但弄坏了别的」？
    43. 终态 `timeout` 在闭环里可能发生在 patch 阶段还是 verify 阶段？
    44. 补丁应用器独立于改码 Agent——这一分层对回滚与重试有何好处？
    45. 改码 Agent 直接写文件 vs 先产出 JSON 补丁再由应用器执行——trade-off 是什么？
    46. 改码输出强制 JSON 而非自由 diff 文本——对闭环稳定性有何价值？
    47. strict JSON → 宽松 JSON → 正则抽取的多级解析降级——哪一级失败会进入 retry prompt？
    48. 某轮改码 parse 完全失败，是否仍应进入 verify？还是短路 exhausted？
    49. 闭环里「反思」是否包含 CoT 链？为什么不推荐？
    50. 半开状态需要连续 2 次 probe 成功才 CLOSED——单次成功就恢复会有什么问题？
    51. 429 Too Many Requests 的 Retry-After 头——固定退避 vs jitter 退避各防什么？
    52. Shell 环境变量白名单——允许 PATH、LANG，拦 API_KEY——漏放行一个变量的风险？
    53. circuit_opened 事件 burst——应告警 infra 还是告警 prompt 导致 parse 雪崩？
    54. 预算耗尽时，降级到小模型还是直接拒绝
    55. 必填参数缺失时，让模型补问还是直接 fail
    56. CLI Agent 和 Web Agent，状态持久化怎么做更合适
    57. 如果要提升模型响应速度，从 Agent 链路哪些地方优化
    58. 怎么量化 Agent 的整体性能，不只看成功率
    59. Gate 重复 read 检测，会不会误伤探索性阅读
    60. Mimo、GLM、DeepSeek 等模型选型时，你会看哪些维度
    61. L2 结构化 final 输出与 L1 ReAct 工具循环——同一 Agent 实例上两种模式如何共存？
    62. 超长 suspect 列表——schema 应否限 top-k？prompt 如何写？
    63. 模型返回 Markdown 包裹的 JSON，解析层怎么兜底
    64. 人工改完 Agent 结论后，要不要写回 Memory？怎么避免污染
    65. Working Memory 容量有限时，Agent 怎么决定「当前回合真正需要记住什么」
    66. 主观偏好（「我喜欢简洁回答」）和客观事实（「我在东八区」）要不要分库、分字段存
    67. 重要性衰减除了 LRU/TTL，能不能用「久未召回 + 低引用」联合打分
    68. 团队级 Memory、项目级 Memory、用户级 Memory 三层怎么叠？写权限怎么定
    69. 多 Agent 共享一块 Memory 时，谁有写权限？子 Agent 能不能写全局 User Memory
    70. 新用户冷启动没有 Memory 时，Agent 行为怎么设计才不显得「失忆」
    71. 模型自我反思后更新信念（「我之前理解错了」），算不算合法 Memory 更新
    72. task_summary 用轻量模型一句话生成——失败时截断 user 文本降级够吗？
    73. Durable 冲突类型 None/Equivalent/Override/Invalid——各对应什么写入动作？
    74. Semantic 仅对最近约 20 条 encode——Older notes 如何仍可能被召回？
    75. read/write/search/shell 成功后更新 Working+Episodic——失败工具是否写入？为何
    76. 置信度随 days_since_seen 衰减——低于阈值不参与召回会否「遗忘」重要决策？
    77. Agent 怎么在 Tool 和 Skill 之间做选择
    78. 工具、Skill 数量很多时，怎么提高调用命中率
    79. 模型编造不存在的工具名，怎么防
    80. 模型不按 schema 填参数、参数胡填时，校验层应该放哪一层
    81. 工具参数经常错：该改 schema、改 prompt，还是加校验 / Gate
    82. 工具重复调用检测，和 Gate 去重是一回事吗
    83. 同一步多个工具部分成功时，Observation 怎么合并反馈给模型
    84. Skill 要稳定执行，除了 prompt 还要靠哪些工程手段
    85. 工具是直接暴露给模型，还是通过服务端统一分发
    86. 工具如何注册、管理、调用？权限、配额一般分几层做
    87. 只读工具和写操作工具，在注册、描述、权限上为什么要分开设计
    88. 两个工具能力重叠时，怎么避免模型随机选、怎么在 schema 层消歧
    89. 并行写工具（同时 patch 两个文件）会有什么竞态？编排层怎么防
    90. 长耗时工具（分钟级测试、打包）该同步阻塞还是异步 Job + 轮询
    91. `grep` 类确定性搜索工具和语义搜索工具，描述里怎么写才不让模型混用
    92. 同一工具连续失败，指数退避和换工具/换参数，Agent 层怎么决策
    93. Skills 仅索引进 prefix、全文按需注入——与「Skill 块写入 user/system」如何配合？
    94. 多 Skill 同时命中同一 issue 时，priority 数值与「最长 pattern 优先」如何组合保证确定性？
    95. 无任何 Skill 命中时，流水线行为是什么——静默继续还是走默认策略 prompt？
    96. 新增一个 L2 域工具，通常需改哪些层（dataclass、registry、权限表、manifest、测试）？
    97. search 工具的子串匹配与正则模式——各自适用什么检索场景？
    98. ToolGroup 式组合工具（如 read + ast 一次调用占一次 quota）——解决什么调用模式问题？
    99. Gate 1 allowed_tools 白名单——与角色注册表移除 write 工具是双重防护吗？
    100. Gate 2 工具存在性检查——动态 register_tool 与静态 registry 如何一致？
    101. Gate 3 参数 validate（含路径逃逸）——与 ToolContext resolve 的分工？
    102. Gate 5 语义 duplicate（同工具同路径重复 read）——解决模型哪种无效循环？
    103. Gate 7 审批时的 diff 预览——应展示多少上下文才算 informed consent？
    104. Gate 9 执行后快照 diff——发现非预期变更时应拒绝结果还是仅告警？
    105. 工具错误 content 以 `Error:` 前缀返回——模型误读为 observation 成功时如何 prompt 约束？
    106. 知识卡片是什么？和 Chunk、FAQ 卡片有什么区别
    107. 知识卡片和 FAQ 卡片在 Agent 里怎么用、适合什么知识类型
    108. 检索为空、检索矛盾、检索过期，三条降级路径怎么设计
    109. 每 step 开始前、模型返回后、每次 execute_tool 前检查——漏一处的影响？
    110. Agent 的停止条件一般设哪几类（成功、失败、超时、人工、无进展）
    111. Agent 最大迭代次数设多少？依据是什么
    112. 怎么判断 Agent 是在有效推理还是在空转
    113. 空转检测怎么定义？连续几步无进展该停
    114. Agent 出现死循环或无限调工具时，最大迭代次数和循环检测怎么设计
    115. Reviewer 和 Executor 互相推翻时，怎么加停止条件避免震荡
    116. 超时策略：设整任务超时还是单步超时？各适合什么场景
    117. Token 预算和步数预算同时耗尽，先停哪一个、对用户报什么
    118. 指数退避 + 抖动（jitter）在 Agent 多 Worker 场景下防什么
    119. 逃逸回归 Case（读 `/etc/passwd`、外网 curl、fork 炸弹）——通过标准是什么？
    120. Agent 的「可靠性」和「安全性」分别解决什么问题？防线一般分哪几层
    121. 怎么防止模型调用危险工具（删库、发信、转账）
    122. 审批结果要不要写回 Memory？写回时怎么避免污染
    123. 覆盖矩阵「5 类错误 × 2–3 难度 → 10 Case」——盲区可能在哪里？
    124. 扩展到 Case 011–020 时，应用什么矩阵原则避免堆重复 TypeError？
    125. composite Case（如多文件 import + 类型错误）——相对单点 bug 多测什么能力？
    126. 负样本 Case（ambiguous issue、期望 exhausted）——要不要建、如何定义通过？
    127. LLM 非确定性下 n=3 重复——均值/方差/report 置信区间要不要报？
    128. Skill 命中率 dashboard 聚合 case_id——低命中是 Skill 问题还是 Case 标签问题？
    129. Agent 成功率除了「任务完成」，还应看哪些指标（步数、成本、副作用、回归）
    130. 负样本 Case（不应修复、不应调用工具）要不要单独建指标
    131. Bad Case 应该怎么分级（P0 / P1 / P2）？依据是什么
    132. Bad Case 闭环流程怎么走：从发现、归因、修复到上线验证
    133. 回答里每句是否都能追溯到 Chunk / 工具 Observation，怎么自动抽检
    134. 灰度上线除了成功率，还要盯哪些 leading indicators（步数、工具错误率、拒答率）
    135. 某角色 parse 失败是否应阻塞整个 repair，还是允许带错误进入下一阶段？

- 系统架构与 Multi-Agent 设计
    1. 为什么不用 LangChain / LangGraph？自研多了什么、少了什么
    2. Single Agent 和 Multi-Agent，按哪三个条件选型
    3. Orchestration 层要不要调用 LLM 做路由？和纯规则状态机边界怎么划
    4. dataclass 协议和 Agent 互聊，哪个更可测、更可回归
    5. 多 Agent 系统应该怎么设计？Orchestration 编排的核心职责是什么
    6. 多 Agent 之间通信用消息总线还是共享状态板（Blackboard）？怎么选
    7. 什么是 Blackboard 架构？它和消息总线、共享状态各适合什么场景
    8. Orchestrator 用纯 Python 调度、不让 LLM 决定下一阶段，为什么这样设计
    9. Orchestrator 调 LLM 路由和纯规则状态机，边界怎么划
    10. 意图识别模块要不要单独做，还是交给主模型一并处理
    11. 意图识别用分类模型还是直接用 LLM？各适合什么场景
    12. 意图识别放在网关层还是 Agent 内部？
    13. 并行意图和串行意图，在编排上怎么区分、怎么调度
    14. 并行意图识别和多 Agent 并行执行，是一回事吗
    15. 意图识别怎么设计并行架构
    16. 动态路由：什么时候该开 SubAgent，什么时候单 Agent 就够
    17. Todolist 由谁维护：模型自己还是 Orchestrator
    18. Choreography（各 Agent 自协调）和 Orchestration（中心编排），在可控性和调试上差在哪
    19. 多 Agent 协作用 DAG 编排、有限状态机、事件驱动，三种形态怎么选
    20. Fan-out / Fan-in 模式里，多个 SubAgent 并行探索，结果用投票、合并还是主 Agent 仲裁
    21. 子 Agent 结论互相矛盾时，Orchestrator 该信谁？冲突解决有哪些范式
    22. Map-Reduce 式多 Agent（分片处理再汇总）适合什么任务？Reduce 由谁做
    23. Blackboard 上多人写同一字段，乐观锁、版本号、单写者原则，怎么避免写花
    24. Orchestrator 本身无状态、状态全外置，和有状态 Orchestrator，怎么选
    25. 意图体系（Intent Taxonomy）怎么设计才不爆炸？层级意图和扁平标签怎么选
    26. 意图识别要不要做 Slot Filling？槽位缺失时是追问还是先路由
    27. 路由到不存在的 Agent / 已下线 SubAgent，Orchestrator 怎么兜底
    28. 入口层意图识别和 Agent 内二次意图识别，会不会重复、怎么分工
    29. 串行意图有依赖（先查状态再执行操作），依赖图在 Orchestrator 里怎么表达
    30. Todolist 和 Blackboard 上的任务状态，会不会双写不一致？以谁为准
    31. 超长输入预处理（日志截断、结构化）后，意图识别用原文还是摘要版
    32. 为什么采用 Layer 1（Agent 运行时）+ Layer 2（修复流水线）双层架构，而不是单层 monolith？
    33. Layer 1 和 Layer 2 的职责边界分别是什么？谁负责 while 循环，谁负责阶段机？
    34. 为什么从零自研 Agent 运行时，而不直接使用 LangChain / LangGraph / AutoGen？
    35. 自研运行时相比成熟框架，你多了什么、 intentionally 缺了什么？
    36. 四个 Agent 是「真 Multi-Agent」还是「四个 prompt 换皮」？本质区别在哪里？
    37. 若只用 Single Agent + 大 prompt，能否覆盖当前能力？Multi-Agent 分工的不可替代性是什么？
    38. `agent_runtime/` 与 `src/` 的模块边界如何划分？哪些能力禁止跨层引用？
    39. `Agent` 运行时为何定义为「单实例生命周期」，而 Multi-Agent 由 L2 工厂创建多个实例？
    40. ReAct 循环为何放在 Layer 1，修复阶段机为何放在 Layer 2，而不是合并成一个 mega-loop？
    41. 标准库零依赖（无 LangChain）这一约束对架构扩展性造成了哪些明确 trade-off？
    42. 为什么 Orchestrator 用纯 Python 编排，而不把阶段流转交给 LLM 或 LangGraph？
    43. 为什么 Agent 之间用 dataclass / Blackboard 结构化交换，而不是 Agent 互相对话？
    44. Blackboard + Orchestrator 状态机 vs LangGraph StateGraph，你会如何对比取舍？
    45. 四个角色（定位 / 检索 / 改码 / 验证）各自的核心产出数据结构是什么？
    46. typed dataclass / JSON 作为交换协议，对可测试性有什么好处？
    47. Blackboard 在整体架构里补足了流程层固定 schema 的什么缺口？
    48. 什么信息应放进流程层固定字段，什么信息更适合放 Blackboard？
    49. 未来若引入 Planner 角色，它与当前「解析 issue + 匹配 Skill」的分工边界是什么？
    50. Blackboard 的 `resolve_conflict` 应采用「最新写入胜出」「source 优先级」还是「Orchestrator 合并」？边界依据是什么？
    51. Blackboard 中间结论注入 prompt 时，应用前缀订阅批量拉取还是 Orchestrator 手工拼接？
    52. Agent 运行时用显式有限状态机（FSM）和隐式 while-loop，对停止、恢复、可测性差在哪
    53. 无效状态迁移（例如未 verify 就标记 success）怎么在 Orchestrator 层硬拦
    54. Multi-Agent full 30/30 而 Single 29/30——这 1 个失败 Case 应如何归因分析

- Context、Prompt 与缓存工程
    1. 跨租户 Prompt Cache 或 embed cache 复用，有哪些数据残留风险
    2. 「换四个不同 system prompt」与「四个独立 Agent 实例」在工程上差在哪？
    3. 什么是 Loop Engineering？它和 Prompt Engineering 差在哪
    4. L1 的 `ContextManager.build()` 与 L2 Orchestrator 手工拼 prompt 为何长期并存？统一的目标形态是什么？
    5. canonical `history.jsonl` 只追加不修改——这一约束对架构可维护性意味着什么？
    6. issue/stack 钉扎区为何属于 L2 编排关注点，而不是 L1 Context 的通用能力？
    7. L0–L5 压缩管线中，哪些级别属于 L1 已实现能力，哪些属于架构目标？面试中如何界定「设计完成 vs 代码完成」？
    8. 多轮对话里用户改口了，Agent 怎么跟（约束优先级、摘要、显式覆盖）
    9. 模型不确定时，该不该主动追问用户
    10. 澄清问题问太多，用户体验怎么平衡？上限怎么设
    11. Layer 1 的 prefix 段 prompt 与 Layer 2 各角色 system prompt 的职责边界是什么？
    12. User Message 模板化（任务描述 / 引用块 / 输出格式分离）——对多 Agent 流水线的好处？
    13. 用户点踩、重问、立刻改口，这些隐式信号怎么进质量看板
    14. 多轮对话里，哪些内容适合做成可缓存前缀

- 结构化输出与解析
    1. Pydantic / schema 校验失败，反馈给模型怎么写才有效
    2. 怎么保证大模型稳定输出标准 JSON
    3. temperature 设为 0 就能稳定输出 JSON 吗
    4. 做工具调用时 temperature / top-p 应该怎么设？两者同时调有什么经验区间
    5. JSON Mode 和 Tool Call 返回结构化数据，怎么选
    6. JSON 修修补补（repair）会不会掩盖模型本身的问题
    7. 为何 Agent 间交换 typed JSON / dataclass，而不是自然语言消息？
    8. SuspectList 典型字段（文件路径、行号、理由、置信度）——哪些是必填、哪些可缺省降级？
    9. 多级 parse 降级：strict JSON → json5 → regex 抽 `{...}`——每一级的适用场景与误判风险？
    10. markdown 代码围栏内的 JSON 提取——模型爱加解释文字时为何需要这一层？
    11. regex 抽取 JSON 作为最后一搏——可能截错嵌套括号时的兜底策略？
    12. 空 SuspectList 合法吗——Orchestrator 应终止还是进入 Retriever 救场？
    13. 若只能保留一种机制（外置 Prompt / YAML Skill / 多级 JSON parse），你会留哪个？对 FixLoop 流水线为何？

- 记忆工程
    1. 预热实例复用 prefix 与 memory 投影，对多角色并行启动的顺序有何要求？
    2. 每个 L2 Agent 独占一个 L1 session 时，跨 Agent 的 memory / similar_fixes 如何共享而不共享 tool history？
    3. 向量记忆和结构化记忆分别适合存什么、各解决什么问题
    4. 显式 Memory 工具（模型主动 remember）和管理员自动写入，哪种更可控
    5. 怎么防止错误记忆污染后续推理（校验、置信度、人工审核）
    6. Memory 遗忘机制怎么做：LRU、TTL，还是按重要性衰减
    7. 匿名用户和登录用户的 Memory 怎么隔离、会话结束后怎么处理
    8. 同类 Agent（如 Claude Code）的记忆机制是什么？错误恢复机制怎么设计
    9. 语义记忆（Semantic）和情节记忆（Episodic）在存储形态、检索方式上有什么本质差别
    10. 记忆要不要带来源（谁说的、哪轮、哪条工具结果）和置信度？检索时怎么用
    11. 用户前后矛盾时（先说要 A 后说要非 A），记忆层检测冲突后怎么处理
    12. 时间敏感记忆（职位、地址、项目状态）要不要 TTL？过期后检索到旧事实怎么办
    13. 近重复记忆（同一事实多种表述）怎么去重、规范化（canonicalization）
    14. 四层记忆（Working/Episodic/Durable/Semantic）的读写 hook 为何挂在 L1 runtime，而非 L2 Orchestrator？
    15. `derive_embed_query` 把「用户全文」与「检索 query」分离——这是 Memory 层 invariant 还是 Context 层实现细节？
    16. 怎么避免 Agent 记住错误结论（Memory 写入门槛、可撤销、ground truth 校验）
    17. Context、Prompt、Memory 三者在架构上分别扮演什么角色？为何 Memory 不能替代 canonical history？
    18. 什么是 Memory Engineering？它和 Context Engineering 怎么分工、边界在哪
    19. Agent 记忆系统应该怎么设计（总体架构：读路径、写路径、生命周期）
    20. Agent 如何做记忆管理？短期记忆和长期记忆在架构上怎么分层
    21. 如何设计四层 Memory（Working / Episodic / Durable / Semantic）的读写路径
    22. 短期 Session Memory 和长期 User Memory，边界怎么定
    23. 长期记忆存在哪：文件、Redis、向量库、OLTP，怎么选
    24. 长期记忆怎么检索：纯向量、关键词，还是混合检索
    25. 用户画像应该放 System Prompt 还是放 Memory？依据是什么
    26. 用户画像放短期记忆还是长期记忆？什么情况下要放短期
    27. 为什么有时要把用户画像做成短期记忆，而不是一直常驻 System
    28. 用户画像放 System 还是 Memory，对 Prompt Cache / Prefix Cache 有什么影响
    29. 结构化存储用户画像：用表结构还是文档模型？怎么设计
    30. 记忆写入时机是每轮都写，还是命中规则才写
    31. 什么是 Episodic Memory Encoding？写入时机和内容怎么定
    32. Memory 写入要不要异步？异步后会不会丢一致性、怎么兜底
    33. 记忆冲突时，新记忆覆盖旧记忆还是并存？合并策略怎么定
    34. dry-run 模式下，哪些副作用必须禁止（含 Memory 写入）
    35. 记忆召回 Top-K 设多少？怎么根据场景调参和评测
    36. 中期摘要丢失了短期记忆中的关键事实，如何设计来保证推理真实性
    37. Memory 多租户隔离：目录、向量索引、embed cache 各层分别怎么做
    38. Mem0、Zep 这类记忆方案和自研记忆层怎么选
    39. 声明式记忆（Declarative）和程序性记忆（Procedural）在 Agent 里分别指什么？Skill 算哪种
    40. 记忆检索要不要做时间加权（更近的优先）？和语义相似度冲突时怎么排
    41. 召回记忆写入 Prompt 时，条数、排序、引用格式（带 ID / 带来源）怎么设计
    42. 记忆注入会不会导致 over-personalization（过度迎合历史偏好）？怎么防
    43. 记忆压缩任务和遗忘任务，是实时做还是定时批处理？对读路径有什么影响
    44. 用户要求「忘记这件事」时，硬删、软删、匿名化三种做法合规和工程上怎么选
    45. 记忆召回的 Precision / Recall 怎么定义？怎么评测「该记的都记了、该忘的都忘了」
    46. Durable Memory 四个固定 topic 各自存什么？为何不允许 LLM 自由新建 topic？
    47. topic.md 条目用分隔符 upsert——冲突时按 subject 首行合并的规则够吗？
    48. MEMORY.md 索引文件的角色——与 topics 目录下正文如何分工？
    49. repair 成功后写入 repair-precedent topic——与 similar_fixes 字段如何闭环？
    50. similar_fixes 只作 Patcher hint——为何不能替代 Localizer 的证据链？
    51. Candidate schema 中 source 权威序（user_explicit > implicit > inferred > system）——为何重要？
    52. Semantic Memory cosine 阈值约 0.3——调高/调低对 recall 与噪声的影响？
    53. Memory Dream 后台任务（idle / repair 结束：去重、过期、index 重建）——为何不阻塞交互？
    54. 记忆健康 metric（条目数、重复率、平均 confidence、Dream 耗时）进 report——怎么用？
    55. Semantic Memory 检索挂掉——Retriever 侧是否依赖它？keyword + git 仍够吗？
    56. 若只能优化一处 Context/Memory 设计（预算、检索、压缩、写入闸口四选一），你选哪处？依据？
    57. Memory 写文件和 Memory 写向量库，各适合什么类型的数据
    58. 多用户 Memory 隔离，向量库要分 index / collection 吗
    59. 多用户共享知识库时，个人 Memory 和公共知识怎么隔离
    60. 记忆占用的 Context 预算和 RAG 检索段怎么分？会不会互相挤掉
    61. 热 / 温 / 冷 三层记忆（Redis → 向量库 → 对象存储）怎么定晋升和降级规则
    62. issue 原文与 stack trace 作为钉扎区注入 prompt——为何不能依赖 Memory 的 relevant 段替代？
    63. 从对话里抽取可写入记忆的内容，规则抽取和 LLM 抽取各适合什么、怎么控幻觉
    64. 多租户场景下，Memory 索引怎么做隔离（目录、命名空间、权限）

- 工具、Skill 与协议生态
    1. 模型一轮返回多个 tool call，执行顺序由谁决定
    2. 空响应、纯文本、tool call 混在一起时，解析层怎么处理
    3. Agent 常见设计模式有哪些（ReAct、Plan-Execute、Tool Loop、Reviewer 等）
    4. ToolGateway 为何独立成中间件层，而不是在每个 Tool 里写 if role 判断？
    5. `ToolGateway` 与九道 `ToolExecutor` 闸口的关系是什么？为何需要两层而不是一层？
    6. YAML Skill 匹配发生在流水线的哪个时点？匹配结果写入哪层状态？
    7. Skill 的 suggested_tools 建议链对闭环重试有何帮助？
    8. 若只能加一个质量闸口（AST 语义 / 静态 lint / 更小 diff 约束 / 更强 schema），你会选哪个？为什么？
    9. 什么是 Tool Use？模型怎么知道该调哪个工具
    10. 什么是 Function Calling？模型侧需要学什么、推理时发生什么
    11. 什么是 Tool Schema？input/output schema 谁来定义、谁来校验
    12. Function Calling、工具调用和 Prompt 里让模型「假装调 API」，本质区别是什么
    13. 工具执行循环（Tool Loop）一般怎么跑：规划 → 调用 → Observation → 再推理
    14. 一个 Agent 里放多少个工具比较合适？工具太多会带来什么问题
    15. 工具描述太长或太相似时，模型选错工具怎么办
    16. 工具太多时，动态工具选择 / Tool Routing 一般怎么做
    17. 大模型如何加载 Skill / 工具描述？上下文会不会被撑爆
    18. Agent 的「一步」怎么定义？一步里允许多个工具调用吗
    19. 并行工具调用和串行工具调用，各适合什么场景、怎么选
    20. Middleware 在 Agent 链路里一般放哪几层、常用来做什么
    21. Agent Hook 挂在哪些生命周期点？Pre-Hook 和 Post-Hook 的典型用途
    22. 工具 enum 值被模型瞎编时，Gate 层怎么拦截
    23. Function Calling 返回空 arguments，应该怎么处理
    24. 如何保证工具参数准确？工具调用经常出错时如何系统性提升正确率
    25. 怎么限制工具调用次数和可调用的工具类型
    26. 工具返回内容应该用结构化 JSON 还是自然语言？怎么选
    27. 工具返回内容太长，截断策略怎么设计（头尾、错误行、结构化摘要）
    28. 工具返回 10MB 文本，怎么截断还不误导模型
    29. Observation 太长时，保留头尾还是保留错误行
    30. 工具链 A→B→C 执行时，中间结果要不要持久化
    31. 工具调用结果能不能缓存？TTL 怎么设？命中时要不要告诉模型「来自缓存」
    32. 工具执行成功但业务失败时，错误码和 Observation 怎么设计
    33. Skill 写得好不好，应该看哪些标准
    34. Skill 怎么沉淀成可复用资产
    35. Skill 如何做版本管理？更新、迭代、撤回时线上怎么无缝动态切换
    36. Skill 召回率 / 命中率怎么测
    37. 什么是 MCP？它和 Function Calling 是一回事吗，是替代还是互补
    38. MCP Server 和 MCP Client 各自负责什么
    39. Skill、MCP、Function Calling 三者在架构里分别扮演什么角色
    40. CLI 和 MCP 谁更像 Agent 的长期入口？会不会互相取代、各自适合什么场景
    41. 什么是 A2A 协议？它解决 Agent 之间的什么问题
    42. 工具白名单和工具黑名单，默认策略应该怎么定
    43. 为什么要有 ToolGateway？工具里写 `if role` 为什么不可维护
    44. 如何设计 ToolGateway：角色权限、配额、审计怎么在一层做完
    45. 模型说「已完成」但工具实际没跑，怎么防（幻觉执行）
    46. 工具执行超时，应该 kill 进程还是等它跑完
    47. 工具调用失败三次后，Agent 该换策略还是直接结束
    48. 用户中途取消任务时，进行中的工具调用怎么收束
    49. 工具粒度怎么定：粗粒度「一站式工具」和细粒度「原子工具」，各适合什么 Agent
    50. 工具命名和描述本质上是不是 Prompt？
    51. Skill 和 Tool 的本质差别是什么：Skill 是「流程知识」还是「另一种工具」
    52. Skill 和 Tool 能不能组合成「Skill 编排多个 Tool」？和 Workflow 边界在哪
    53. 怎么评测 Tool Selection 准确率？除了端到端成功率还有哪些离线指标
    54. Tool Call 的 Precision / Recall 怎么定义？误调工具和漏调工具哪个更致命
    55. 工具返回的错误要不要分类成 retryable / non-retryable？对 Agent Loop 有什么影响
    56. 工具执行中间状态要不要流式回传给用户（进度条），和 Observation 怎么区分
    57. Patch / Edit 工具和整文件重写工具，为什么 Code Agent 常优先前者
    58. 大模型怎么加载 Skill / 工具描述？工具多了上下文会不会爆
    59. Tool Calling 和把 API 写进 Prompt 让模型生成调用，为什么前者更稳
    60. Function Calling、Prompt 调工具、MCP 调用，本质差别是什么
    61. Skill 的定义是什么？触发正则、策略文本、建议工具链、示例 patch 各自解决什么问题？
    62. `suggested_tools` 写入 Skill 提示——模型未按建议调用工具时，Orchestrator 是否干预？
    63. Skill 注入 prompt 的 `[Skill 提示]` 块应包含哪些字段，哪些不应过长？
    64. Skill 命中结果写入 eval report 的 `matched_skill`——如何用于 Skill 命中率 dashboard？
    65. 自动校验层在闸口之前还是之后——能拦截哪些模型「格式对但语义错」的参数？
    66. 九道闸口的顺序为何固定——调序（如 quota 与 duplicate 对调）会破坏什么？
    67. 任一闸口失败返回结构化 ToolExecutionResult、不抛异常——对 ReAct 循环的设计理由？
    68. context token 硬顶 8000 与工具返回 L1 截断——两层如何共同防 observation 爆炸？
    69. 全局 `-dry-run` 下工具链是否仍走九道闸——哪些 gate 行为应变化？
    70. 未来接入 MCP 外部工具——应放在 Gateway 之后还是替换 registry 一层？
    71. 取消信号应穿透 AgentLoop、ModelClient、ToolExecutor——缺一层会怎样？
    72. 设计 cancel 全链路：模型请求、工具执行、容器、锁释放
    73. Checkpoint 应该在工具执行前存还是执行后存？各占什么一致性优势
    74. 高风险 write / patch 工具，审批流应该插在哪一层（Gate、ToolGateway、工具内）
    75. 工具调用的执行沙箱一般怎么设计（进程、网络、文件系统、权限）
    76. 幻觉从工具链的哪一环产生？工程上能消掉哪一部分
    77. 用户取消时，已发出但未执行的 tool call 怎么 discard，已执行写操作怎么补偿
    78. 流式返回里如何提前解析 tool call 参数（partial JSON / streaming parser）

- RAG 与知识检索
    1. Retriever 检索的是代码/测试/Git 上下文，和企业知识库 RAG 项目的边界在哪里？
    2. Query Expansion 和 Query Rewrite 有什么区别、各适合什么场景
    3. RAG 知识库和 User Memory 在概念上怎么分？同一句话该进 KB 还是进 Memory
    4. keyword 与 semantic 合并而非只取向量——代码修复场景下 keyword 不可替代性？
    5. RAG 还有价值吗？超长上下文模型会不会取而代之
    6. Embedding 模型和 LLM 要不要共用一家？专门选型时看什么
    7. 查询改写（Query Rewrite）如何实现？会不会改坏原意、怎么防
    8. HyDE 是什么？和直接 Embedding 查询相比，各自败在什么场景
    9. RAG 召回为空时，Agent 应该怎么降级（拒答、联网、追问、纯推理）
    10. 什么是 Agentic RAG？它和普通 RAG、一次性检索注入差在哪
    11. Agentic RAG 和一次性检索，多跳任务何时必须用前者
    12. 什么是 Self-RAG？流程里的 self-reflect 怎么做、起什么作用
    13. 什么是 GraphRAG？适合什么类型的知识、和向量 RAG 怎么配合
    14. 代码 Agent 为什么常优先 Grep / Read，而不是纯向量检索
    15. Claude Code 为什么偏 Grep / Read 工具，而不是 RAG 检索代码库
    16. 知识库很小（几十页）时，全量塞进 Context 和建向量索引，哪个更划算
    17. 多租户知识库，共享底座 + 租户隔离索引，和每租户独立实例怎么选开么
    18. Retriever Agent 的职责边界——「代码/测试/Git 上下文检索」与企业文档 RAG 有何本质不同？
    19. Retriever LLM 路径：模型通过 read/search/find_test/git 工具收集后再输出 JSON——与 RAG 向量召回路径对比？
    20. Retriever LLM 超时降级：堆栈文件名 + ripgrep 规则检索——保底哪些字段？
    21. 代码检索为何以 ripgrep/search 为主、而非 embedding 文档 chunk——FixLoop 场景假设是什么？
    22. 查询改写（Query Rewrite）会不会引入新幻觉？怎么防改坏原意
    23. 什么是 HyDE？用假答案做检索时，幻觉风险怎么控
    24. 什么是 RAGAS？Context Precision / Recall 等各表示什么、哪些指标最值得先看
    25. 内在幻觉（Intrinsic）和外在幻觉（Extrinsic）分别指什么？RAG Agent 哪种更常见

- 代码修复流水线与自愈闭环
    1. 在不增加 max_retries 的前提下，提升 patch 质量的三个杠杆是什么？
    2. L2 node_timings（localize_ms、retriever_ms、patcher_ms、verifier_ms）——哪段 P99 高应先查什么？
    3. 难度重标定（依据历史跑批上调难度或标 `requires_retriever`）——如何避免主观拍脑袋？
    4. 若重做 FixLoop，最先改 architecture 的哪一块？为什么不是别的模块？
    5. 编排器为何被设计为纯 Python 组件、不直接调用大模型？
    6. Agent 间通信用自然语言对话，FixLoop 为何明确拒绝这种主路径？
    7. 流程层状态与单次 ask 的运行层状态，生命周期有何不同？
    8. 编排器如何把上一阶段结构化产出，拼装进下一阶段的 prompt？
    9. 验证结果字段如何驱动编排器进入重试环或终态？
    10. `pending` 状态之后，编排器的第一步通常做什么（解析 issue、匹配策略等）？
    11. 编排器组装的 `feedback` 字段与 `agent_errors` 字段，职责如何划分？
    12. 子任务拆分（大 Issue 拆成多个 subtask）在状态层如何表示与汇总？
    13. 复合类 Issue 为何强制走满四角色，而简单 import 类可否裁剪检索阶段？
    14. 动态 Agent 裁剪由编排器规则决定，还是由某个 Agent 自决？
    15. Skill 的 priority 与最长 pattern 规则，解决多策略冲突的什么场景？
    16. 无 Skill 命中时，编排器默认行为是什么？
    17. 标准流水线各阶段的先后关系是什么？哪些可并行、哪些必须串行？
    18. 定位与检索并行时，两者为何都属于只读阶段？
    19. 并行阶段使用线程池而非 asyncio，FixLoop 的考量是什么？
    20. 并行阶段中，若定位先完成、检索仍在进行，编排器应如何等待与合并？
    21. 并行阶段一方超时，编排器是整体失败还是带着部分结果继续？
    22. 检索产出为空时，改码阶段是否仍应启动？依据是什么？
    23. 定位与检索对同一文件路径结论不一致时，合并策略是什么？
    24. 合并后的嫌疑位置与检索上下文，如何一起注入改码 prompt？
    25. 取消发生在改码或验证阶段，编排器与子 Agent 的协作顺序是什么？
    26. 某阶段超时后，编排器应尝试降级、重试还是直接 failed？
    27. Single-Agent 基线变体，编排器是合并角色还是只调度一个实例？
    28. 多个 repair 并行跑在同一 repo 上，流程层状态隔离与写冲突由哪几层共同保证？
    29. 若扩展第五个角色（如 Planner 或 Reviewer），最先应改状态 schema 还是改流水线拓扑？
    30. 与 Reflexion、Self-Reflection 等「让模型反思」方案相比，FixLoop 的「反思」载体是什么？
    31. 验证失败后，编排器组装的结构化 feedback 通常应包含哪些字段？
    32. 默认最多 3 轮 patch→verify 重试——这一上限如何权衡成本与成功率？
    33. `retry_count` 在什么时点递增？什么条件下停止进入下一 retry？
    34. 重试环是否重新跑定位/检索阶段，还是只重跑改码+验证？依据是什么？
    35. 快照应在「应用补丁之后、启动验证之前」还是「应用补丁之前」？FixLoop 选哪种？
    36. read_file 工具在 repair 流水线中的典型用法——与 ast_parse 的分工？
    37. QuotaEnforcer 的 writes ≤20、shell ≤10、total ≤50——单 session 口径对 long repair 够吗？
    38. resume 后从「下一步」继续 vs 重放最后一步——FixLoop 选哪种？为什么？
    39. 补丁在宿主机 apply 还是在容器内 apply——FixLoop 选哪条路径？为什么？
    40. Pass@k（pass@1 / pass@3）——FixLoop 默认 3 次重复与 Pass@k 报告如何对应？
    41. 用一句话说明 FixLoop 解决什么问题、明确不解决什么问题？
    42. FixLoop 与 Cursor Agent / Claude Code / Devin 的定位差异是什么？你刻意没做哪些能力？
    43. 若将 FixLoop 泛化为「通用代码 Agent」而非「测试失败修复」，最先突破的是哪条项目边界？
    44. FixLoop 中「真 Multi-Agent」的三要素是什么（工具集合、提示词、运行时实例）？
    45. 四个 L2 角色是否共享同一个 L1 session？不共享的设计动机是什么？
    46. 各 Agent 返回的非结构化自然语言，编排器如何处理才能进入状态机？
    47. 若用 LangGraph 建模 FixLoop 流水线，节点与边应如何对应现有 phase？
    48. FixLoop 中「工具」的唯一定义源是什么？参数 schema、校验逻辑、执行体如何保持一致？
    49. 编排器手工拼 prompt 与 L1 上下文管理器五段组装，长期应如何统一？
    50. 权限网关（ToolGateway）与编排器阶段锁，是否功能重复？如何分工？
    51. 工具链 quota 耗尽时 repair 应 exhausted 还是降级无工具 Single-Agent？
    52. 为什么 Agent 之间不互聊，而用结构化状态（dataclass / RepairState）交换
    53. 多 Agent 流水线里，状态存在哪、由谁写？Blackboard scratch 和终态 RepairState 信息放哪
    54. 模型返回内容质量如何保证（校验、Verifier、重试、人工抽检）
    55. 代码修复 Agent，ground truth 为什么常选 pytest
    56. Verifier 独立 Agent 和 pytest 脚本，ground truth 怎么定
    57. 为什么验证默认走 Docker 而不是宿主机
    58. Patch 太大时，怎么限制改动范围
    59. patch apply 失败，全量回滚还是部分回滚
    60. 为什么验证用 Docker 沙箱，而不是宿主机直接跑 pytest？Eval 与 repair 路径为何可能不同？
    61. `Agent.ask()` 与 `Orchestrator.run_repair()` 的调用关系是什么？一次 repair 会触发几次 ask？
    62. Localizer 与 Retriever 并行时，Orchestrator 如何保证阶段边界不被 LLM 打乱？
    63. `RepairState.phase` 与 `TaskState.status` 分别属于哪一层？为何不能混用一个状态枚举？
    64. Blackboard 与 RepairState 的职责分工是什么？什么信息进 Blackboard，什么信息只进 RepairState？
    65. M5–M8 里程碑划分的架构含义是什么？Layer 2 为何按 Orchestrator / agents / repair / eval 拆模块？
    66. 定位角色为何只能读和分析，不能写文件或跑沙箱？
    67. 检索角色为何只能Gather上下文，不能生成或应用补丁？
    68. 改码角色为何持有写权限却不能触发容器验证？
    69. 验证角色为何只能操作沙箱，不能修改工作区源码？
    70. 一次 repair 的「流程真相源」是什么？它贯穿哪些阶段？
    71. 改码阶段写入的候选补丁字段，验证阶段如何只读消费？
    72. 一次 repair 的 run 标识，如何与各子 Agent 的运行 trace 关联？
    73. 定位产出为空（无嫌疑文件）时，编排器应终止还是进入改码？
    74. 「唯一写阶段」为何只有改码？定位/检索为何不能顺手改文件？
    75. 阶段级读写锁：定位/检索共享读锁、改码独占写锁——防什么竞态？
    76. 写窗口单飞（同一时刻只允许一个改码窗口）与 Web 层同 repo 写锁如何配合？
    77. localize / patch / verify 分阶段超时，阈值为何不应一刀切？
    78. verify 连续失败后触发 Single-Agent 降级，是编排器策略还是 Agent 策略？
    79. `degraded_mode` 在状态层如何标记？对后续阶段调度有何影响
    80. 编排器解析 issue 得到的 `RepairPlan` 通常包含哪些字段？
    81. issue 原文与 stack trace 作为钉扎区，在编排器拼 prompt 时如何处理？
    82. Agent 池化预热发生在 repair 启动时——预热几个实例、对应哪些角色？
    83. FixLoop 自愈闭环的核心循环是什么？每一轮包含哪些步骤？
    84. 为何不让 Verifier 角色直接改代码再自证？
    85. 多 Agent 同一 repair 的 trace——如何用 run_id + agent 角色拼接成可读的端到端故事？
    86. 为何将 Localizer / Retriever / Patcher / Verifier 的 system prompt 外置为独立文本，而不是硬编码在编排器里？
    87. 分 Agent prompt token 预算（如 Localizer 2k / Patcher 4k）超标时应先砍哪类内容？
    88. 按 issue 类型（如 ImportError vs logic_error）使用不同 patcher prompt 后缀——与 Skill 体系如何分工？
    89. Skill 匹配发生在流水线哪个阶段——Localizer 前、Patcher 前，还是两者都有？
    90. Layer 1 通用工具与 Layer 2 域工具的分工边界是什么？为何 ast/stack/git/find_test 不在 L1？
    91. Verifier 仅暴露 sandbox 验证工具、不暴露读写工具——注册表层面如何实现？
    92. Patcher 注册表为何移除 shell 工具——风险与能力 trade-off？
    93. Localizer 注册表为何移除 write/patch/shell——仅靠 ToolGateway 不够吗？
    94. write_file 追加模式与覆盖模式——Patcher 应偏好哪一种？为何？
    95. 按修复角色（Localizer / Retriever / Patcher / Verifier）组装工具注册表——与「换 prompt 伪分工」有何不同？
    96. Retriever 注册表为何移除 ast_parse/stack_parse——与 Localizer 的能力边界？
    97. search 结果上限与截断提示——对 Retriever 收集上下文的影响？
    98. Gate 4 配额检查——分 Agent 独立计数如何避免 Retriever 读耗尽 Patcher 写额度？
    99. Localizer ∥ Retriever 并行——Retriever 只读工具链为何不构成写冲突？
    100. 若只能加强一条能力（域工具 / 九道闸 / Retriever 降级 / 分角色 quota），你会选哪条？对 FixLoop 为什么？
    101. 文档为何强调沙箱「不保证 patch 业务语义正确」——与 Verifier 职责的边界？
    102. config_error 类 Case（如 pyproject 缺段）——与 logic_error 在评测上如何区分？
    103. ImportError Case 与 TypeError Case 的 avg_retries 差异——能否支持 Skill 设计？
    104. logic_error 困难 Case（三跳调用链）——fix_rate 低是定位难还是 patch 难？
    105. 消融 only no_retriever 而不 ablate Localizer——「去定位角色」实验价值大吗？
    106. cancel 收尾时，流程层状态应置为何种终态？还需释放哪些锁？
    107. TaskState 状态机贯穿 L1/L2——status=stopped 与 repair status=user_cancel 在 trace 如何对齐？
    108. L1 checkpoint 与 L2 repair checkpoint——解决的续跑粒度有何不同？
    109. 若 checkpoint 只能存一种粒度（tool 步 / repair 阶段 / 仅终态）——你会选哪种？为什么？
    110. 断点续跑：对话级 checkpoint 和 repair 阶段 checkpoint 怎么分层
    111. 容量规划：100 并发 repair 需要多少 Worker、多少 sandbox 槽位、多少模型 RPM——你会怎么估？
    112. FixLoop 沙箱的四维隔离目标（文件系统、网络、资源、权限）分别指什么？
    113. FixLoop 自建评测体系要解决什么问题——相对「跑通 demo」多证明了什么？
    114. 限流、熔断、降级在 FixLoop 里各自解决什么问题——三者如何配合而不是重复？
    115. Worker 重复消费同一 repair 任务时，怎么做幂等
    116. Ground truth 为什么是容器内 pytest，而不是模型自评或 LLM-as-judge？

- 运行时控制、Cancel 与 Checkpoint
    1. CLI Ctrl+C、Web `POST /cancel`、REPL `/cancel`——如何统一置位同一 token？
    2. 等待模型响应时 cancel：关闭 stream / abort HTTP——半开连接资源如何释放？
    3. REPL 二次 Ctrl+C 与 `/cancel`——不杀进程而只停当前 Loop 的原因？
    4. read/search 类工具 cancel：等待完成 vs terminate——丢失 observation 可接受吗？
    5. write/patch 类工具 cancel：禁止 kill 中途、等返回再回滚 pre_tool 快照——半写风险？
    6. 前端关闭页面时，后端 Agent 要不要自动 cancel
    7. Agent 目标漂移怎么发现、怎么拉回（约束、checkpoint、Reviewer 刹车）
    8. 用户任务进行到一半改口（意图抢占），进行中的 SubAgent 该 cancel 还是做完
    9. CancellationToken 为何必须同时穿透 L1 AgentLoop 与 L2 Orchestrator，而不是只在最外层 kill 进程？
    10. L1 逐步 checkpoint 与 L2 阶段 checkpoint 分别解决什么续跑场景？为何不能只用一种？
    11. 终态 `user_cancel`、`timeout`、`regression` 与 `failed` 的口径差异是什么？
    12. 从某一失败阶段断点续跑时，哪些状态可复用、哪些必须重算？
    13. 取消信号下发后，进行中的定位/检索 ask 如何处理（只读可丢弃）？
    14. 用户取消时写入的阶段 checkpoint，应包含哪些最小状态快照？
    15. 终态 `user_cancel` 时，工作区与已应用补丁应处于什么一致状态？
    16. SSE/chunk 流式输出——对 REPL 首 token 延迟与 cancel 响应各有什么帮助？
    17. WebSocket 备选 SSE——双向 cancel + 心跳在什么规模下值得换协议？
    18. 解析失败重试几次？和 max_steps 怎么叠加、会不会放大成本
    19. 用户刷新页面后续跑任务，服务端要存哪些最小状态
    20. Memory 和 Checkpoint 都存「状态」，概念上怎么分？能不能互相替代
    21. write_file 的原子写（临时文件再替换）——对 cancel 中途回滚的意义？
    22. Gate 8 执行前快照——与 cancel 时 write 类工具「等完再回滚」如何配合？
    23. CancellationToken 与「杀整个进程」——协作式取消的核心差异？
    24. 审批等待人工确认时用户 cancel——应拒绝工具还是记为 user_cancel 终态？
    25. Cancel 信号怎么传到模型的 HTTP 流式请求
    26. 协作式 Cancel 和强杀进程，不同工具类型怎么分策略
    27. write 类工具 cancel 时为什么要等返回再回滚，不能强杀？read 为什么可以直接 discard
    28. Cancel 请求本身要不要幂等？重复点「停止」按钮后端怎么响应
    29. Cancel 与工具完成竞态：工具已写完但 Cancel 已到，以谁为准、怎么对用户解释
    30. Cancel 原因码（用户主动、超时、预算耗尽、策略拦截）要不要写入 trace
    31. 单用户 Session 单飞（single-flight）：新请求来了，旧任务 Cancel 还是排队
    32. L2 `repair_checkpoint.json` + `-resume-repair`——应存 phase 还是仅存产物 JSON？
    33. 双层 checkpoint 同时存在时——用户应优先 `-resume` 还是 `-resume-repair`？
    34. 为什么需要双层 Checkpoint？一层 step、一层 phase 不够吗
    35. L1 逐步 checkpoint 和 L2 阶段 checkpoint，恢复粒度怎么选
    36. L1 step checkpoint 和 L2 phase checkpoint，用户侧怎么感知与恢复
    37. Checkpoint 存全局状态时，模型输出不可预测导致状态不一致怎么办
    38. Checkpoint 存全量状态还是增量（delta）？对存储大小和恢复复杂度有什么影响
    39. Checkpoint 里要不要包含完整 message history，还是只存结构化 state + 指针
    40. Checkpoint 序列化格式（JSON、msgpack、protobuf）选型看什么
    41. 单个 Checkpoint 过大（含大段日志 / 代码）时，外置 blob + 引用怎么设计
    42. Checkpoint 保留多久？TTL、按任务类型分级清理、合规留存怎么平衡
    43. Checkpoint Schema 升级时，老快照怎么迁移、能不能拒绝加载过期版本
    44. 从 Checkpoint 续跑要不要幂等？同一 snapshot 被触发两次恢复怎么防双写
    45. 恢复点存在外部副作用（已发邮件、已写库）时，还能不能「纯续跑」
    46. Checkpoint 存本地盘、对象存储还是数据库，对恢复速度和多实例有什么影响
    47. 预算中途耗尽，能不能「降级续跑」（换小模型）还是必须 Checkpoint 后结束
    48. 运行时控制三板斧：停止（Stop）、挂起（Pause）、续跑（Resume）各自最少需要哪些系统支持
    49. REST v1：`POST /api/v1/repairs` 创建、`GET .../{id}` 查询、`POST .../cancel`——幂等性应落在哪几个 verb？

- 安全、沙箱与 Human-in-the-Loop
    1. 敏感信息防护三层：运行时隔离、输出脱敏、存储/索引排除——各层典型措施是什么？
    2. 什么是 Human-in-the-Loop？哪些动作、哪些节点必须人介入
    3. Gate 事前审批和事后 Audit，对高风险写操作怎么选、怎么配合
    4. Human-in-the-Loop 审批超时，默认拒绝还是自动通过？怎么定策略
    5. Human-in-the-Loop 批处理审批怎么做（合并同类请求、批量确认）
    6. 怎么防止 Agent 读写越界路径（工作区边界、符号链接、路径规范化）
    7. 敏感信息进日志时怎么做脱敏？脱敏规则由谁维护、怎么版本化
    8. 工具里的 Shell / 代码执行，命令注入和参数注入怎么防（白名单、转义、无 shell）
    9. 数据库类工具怎么防 SQL 注入？能不能让 Agent 只能走参数化查询接口
    10. Dry-run 模式贯穿 L1 Tool 与全局 `-dry-run`——架构上如何保证「规划」与「执行」可切换？
    11. 沙箱验证保证环境隔离，为何不保证 patch 的业务语义正确？
    12. `/health` liveness 与 `/ready`（Redis + Docker）——K8s 探针应如何分别配置？
    13. 线上事故 Runbook：模型 429、索引滞后、沙箱镜像漏洞，三类先处理谁
    14. 容器逃逸 Case（读 /etc、外网 curl），验收标准是什么
    15. Dry-run 预览和直接执行，用户信任怎么建立
    16. 日志和 Trace 保留策略（7 天 / 90 天 / 合规 7 年），成本怎么控
    17. 高风险工具（写库、发消息、删数据）怎么做二次确认
    18. Gate 6 dry-run——返回「计划执行」而不改 workspace，对 Agent 循环意味着什么？
    19. 为何不声称 Docker 沙箱「绝对安全」——应如何表述 threat model？
    20. 输出不合规时，应该重生成还是直接拦截？各适合什么场景
    21. Agent 沙箱常见隔离维度有哪些？应该隔离哪些资源
    22. 为什么沙箱不承诺「语义正确」，只承诺「隔离执行」？
    23. 风险评分：低风险自动执行、中风险 HITL、高风险拒绝，分数由什么信号组成
    24. Human-on-the-loop（旁路监督）和 Human-in-the-loop（卡点审批）分别适合哪类操作

- 评测、幻觉与质量保障
    1. Case 库单条目录应包含哪些 artifact（issue、标注 diff、最小行数、元数据、微型 repo）？
    2. `min_lines.txt` 单行整数在指标里扮演什么角色——谁维护、如何验收？
    3. `verified` vs `scaffolded` 状态流转——什么条件下才进正式 60-run？
    4. 正式 60-run 实验设计（变体 × Case × 重复）——各因子水平如何选才可辩护？
    5. by_issue_type、by_difficulty 分桶——发现某类 fix_rate 低后下一步做什么？
    6. 消融实验如何避免 confound（同时改 prompt + skill + 编排）？
    7. holdout Case 集——是否应对 headline 数字保密以防过拟合 prompt？
    8. 多语言 Case（Java/Node）扩展——指标分 language 桶还是混报 fix_rate？
    9. 统计检验：full 比 single 多 1/30 是否 significant——面试中如何诚实回答？
    10. 什么是 LLM-as-Judge？常见偏见和坑有哪些，怎么减轻
    11. 什么是 Pass@k？代码 Agent 为什么常用 Pass@k 而不是单次准确率
    12. Pass@1 不够时，Pass@K 有没有产品意义（重试、多候选、人工挑选）
    13. 「真 Multi-Agent」和「四个 Prompt 换皮」怎么证伪、怎么评测
    14. 选 dataclass 协议而非自然语言协作，trade-off 是什么？对 eval 可重复性有何影响？
    15. 闭环的 ground truth 是什么？为什么不是模型自评或 LLM-as-judge？
    16. 分 Provider 独立熔断状态——eval 并行跑批时如何防止「一家拖全局」？
    17. eval final_report 的 by_agent token 表——能否反哺生产可观测 dashboard 设计？
    18. 对抗样本怎么进回归集
    19. token、latency、cache 命中率如何纳入实验报告与 A/B 对比
    20. 按角色拆分 prompt 对迭代、评测回归、多人协作分别有什么好处？
    21. issue 文本中潜在的 prompt 注入指令——评测侧如何构造对抗 Case？
    22. 一次 Prompt 改动应跑哪些 eval 变体才算「可合并」？
    23. Multi-Query Retrieval 什么时候值得开、成本收益怎么权衡
    24. 评测 Case 为何不直接 symlink demo 仓库——对标注独立性与 Runner 隔离的意义？
    25. 大模型为什么会产生幻觉？幻觉从哪来、分哪几类
    26. 幻觉和胡编引用（伪造 citation / 假文件路径），怎么区分、分别怎么处理
    27. 查询改写和 Multi-Query Retrieval 会不会增加幻觉风险？成本与收益怎么权衡
    28. 怎么定义和统计 Agent 的幻觉率？线上和离线各怎么度量
    29. 代码修复类 Agent 为什么 ground truth 用 pytest 而不是 LLM-as-Judge
    30. Pass@1 和 Pass@K 在 Agent 评测里怎么选？汇报时哪个更有说服力
    31. 回归评测集怎么控制规模，又避免 Prompt / 策略过拟合
    32. 对抗评测集（故意 Injection、越权、诱导幻觉）要不要和功能评测分开跑

- 可观测、Trace 与运维韧性
    1. Agent 运行全流程是什么？从请求进入到落库 / trace 怎么走
    2. 编排全链路 trace：每个 SubAgent 一个 span，run_id / parent_span 怎么串
    3. 每轮 retry 是否应在 trace 里可区分（retry_index、feedback_hash 等）？
    4. 熔断事件（opened / half_open_probe / closed）写入 trace——排障时如何与 model error 区分？
    5. 审计日志（谁、何时、对哪 repo 发起 repair）——与 trace.jsonl 是否重复存储？
    6. trace 超 1000 行 gzip 归档——归档后 Deterministic Replay 是否仍可用？
    7. tool_rejections_by_gate 进 report——与 trace 中 permission_denied 如何交叉验证？
    8. 后端以什么格式推送流式信息（SSE event、JSON chunk、trace 字段）
    9. Tracing 要解决什么痛点？Langfuse、LangSmith 这类产品补哪块
    10. 审计日志需要记录 Agent 的哪些动作
    11. 线上采样率怎么定，又不丢关键 case
    12. Metrics：RED（Rate Errors Duration）套在 Agent 上，三个指标具体指什么
    13. Trace 体量爆炸时，聚合指标 + 采样 trace 怎么配合才不 blind
    14. 结构化日志 schema（run_id、phase、step、latency_ms）为什么要统一
    15. 大体量 trace 存对象存储、索引存 OLTP，冷热分层检索怎么设计
    16. 多实例部署时，trace 怎么串起来
    17. 除了成功率，Saturation 指标该看队列长度、GPU 利用率还是 API 429 比例
    18. 工具 schema 变更后，老会话 / 历史 trace 怎么兼容

- 性能、流式与 LLM 网关
    1. trade-off：准确率、延迟、成本只能保两个，你怎么选
    2. 意图识别错了，应该重问用户还是 silent fallback
    3. 熔断器 CLOSED → OPEN → HALF_OPEN 状态迁移的触发条件分别是什么？
    4. 连续 5 次失败打开熔断、30s 后半开探测——阈值选太小或太大各有什么后果？
    5. 熔断打开时立即拒绝而非等待 HTTP 超时——对 Agent 循环与用户体验的意义？
    6. 主模型与轻量模型是否应共用同一熔断器——Ollama 挂了是否应拖死 Anthropic 路径？
    7. 令牌桶 RPM 限流放在 ModelClient 层——与 eval `workers=N` 并行如何协调配额？
    8. 重试上限怎么设，避免成本和延迟失控
    9. 首 Token 延迟和端到端延迟，优化优先级怎么排
    10. 流式输出首字慢，除了换模型还能做什么
    11. SSE 和 WebSocket，Agent 流式为什么常选 SSE
    12. SSE 和 WebSocket，企业网络环境下怎么选
    13. AI 流式输出选 SSE 的工程理由（前后端交互、代理、断线重连）
    14. SSE 断线重连时，事件怎么补发
    15. SSE 流式输出时用户关浏览器，已输出内容怎么保留
    16. 流式输出一半报错，前端怎么展示 partial result
    17. 怎么设计 LLM 网关：限流、熔断、重试、fallback、计费
    18. 微调（LoRA 等）的价值与成本，什么时候值得做
    19. LLM 请求连接池和 HTTP keep-alive，对高并发 Agent 为什么重要
    20. 首 token 延迟和端到端延迟，SLA 应该绑哪个
    21. 首 token 延迟偏高时，你会优先优化链路的哪一段
    22. Ollama 和 vLLM，本地部署 Agent 怎么选
    23. 同一用户多条 SSE 连接，Sticky Session 必须吗？无状态推送怎么做
    24. KV Cache 是什么？为什么能省成本
    25. Semantic Cache 和 Exact Cache 怎么选、怎么配合
    26. KV Cache 命中率低时，优先改 prompt 结构还是改调用方式
    27. 多个 Skill 可处理同一意图时，优先级、置信度和 fallback 怎么定
    28. 429 限流时，Retry-After 要不要严格遵守？退避策略怎么定
    29. 故障恢复后惊群（大量任务同时重试），入口限流和队列 shaping 怎么做
    30. Web Agent 的最低安全要求是什么（CSRF、CORS、SSE 等）
    31. 无限工具循环、超大 Context 请求，算不算安全 DoS？配额和熔断怎么设

- 系统设计、会话与部署
    1. 多轮对话里意图漂移（上一轮修 bug、这一轮问部署），上下文意图怎么更新
    2. Idempotency-Key 重复 POST 返回同一 repair_id——客户端重试与服务端 dedup 的契约？
    3. 单 Turn 单容器，温池复用会不会造成环境污染
    4. 健康检查：Worker liveness 和 readiness 要不要区分「LLM 上游不可达」
    5. Session 和 User ID 如何绑定？同一用户多设备登录 Session 怎么同步
    6. 有状态 Agent 和无状态 Agent，架构怎么选
    7. 有状态 Worker 和无状态 Worker + 外置状态，怎么选
    8. 任务队列用 Redis 还是 Kafka，怎么选
    9. 单租户部署和多租户 SaaS，隔离做到哪一层
    10. 运行记录落库：runs 表、events 表、artifacts 表，各存什么、为什么事件要 append-only
    11. 离线时先队列缓存 Memory 写入，恢复后重放，幂等键怎么设计
    12. 同一用户多个并行 Agent 任务写 Memory，乐观锁、队列串行化、分片锁怎么选
    13. 三层隔离：project / user / session——Web 多租户下路径如何映射？
    14. Gate 7 审批——write/patch 需人工确认；CLI 与 Web 审批队列如何统一？
    15. 同一请求在 LLM 侧怎么做幂等（request id、去重、结果缓存）
    16. 单 Turn 单容器生命周期（create → 传码 → 执行 → destroy）的设计理由？
    17. 多租户 Agent：计算隔离、存储隔离、网络隔离，最少要做到哪两层
