# 代码探索消费链 MVP：实现与验收

日期：2026-10-01。本轮范围由用户逐项讨论后授权实施：检索语义贯通、消费时复验和显式失效。已有文件/文本检索、Python LSP、局部关系与片段选取继续复用；原 P0–P4 的历史验收不作为本轮实测结果。

## 交付行为

1. `RetrievalResult` 随 Observation 落盘，并经过既有脱敏路径。模型工具结果、XML/native 历史、显式展开、选中片段和 Plan 候选输入保留 execution、completeness、scanned_scope、截断/降级原因、来源/解析级别、query_id、observed_at。空结果只描述已扫描范围，候选不能成为确定运行时调用关系。
2. 消费时按 workspace/session/task/run、已记录文件哈希和 blob 校验。实际读字节受单文件、共享总量、文件数、deadline/取消限制；同轮相同文件复用校验结果，状态目录不作为源码目录。freshness 与 completeness 独立：文件新鲜的 partial 结果仍可使用。单个选中片段的 `snippet_freshness=fresh` 不提升其父查询整体 freshness；父查询保留自己的完整性和来源说明。
3. 已改动、删除、越界、scope 不符或 blob 损坏时拒绝正文，保留原因和 Observation 引用。无完整文件版本、部分命中缺版本或预算不足时为 unknown，不冒充 fresh，也不作为当前源码正文注入。原始历史/blob 供审计；失效只影响本轮投影和既有生命周期。
4. native 工具调用/结果保留原顺序与 ID，失效正文替换为诊断，`partial_result` 等执行反馈保留。XML 的直接工具续接消息及旧 checkpoint 续接消息改用引用，避免绕过复验；失效后重建封印历史并过滤相应文件摘要。恢复后的局部视图仍遵守已有 epoch 重置。
5. 现有 context manifest 增加逐 Observation 状态和校验实际字节/文件计数；不另建探索状态机、数据库、图索引或重规划入口。

主要实现：`code_exploration/consumption.py`、ObservationStore、ContextManager、AgentLoop、局部关系/片段选取和 Plan 的 Observation 消费。Layer 1 没有引入 Layer 2 依赖。

## 借鉴与取舍

依据 [五维参照 R-02/R-03/R-04/R-10](AGENT_DESIGN_REFERENCES_2026-10-01.md)：采用 OpenCode 的按需探索和来源边界、Pi 的模型消息投影边界，复用 FixLoop 的 Observation/校验/manifest。Hermes 的历史检索与经验补充继续后置，避免扩大本轮工作量。

- 一份检索契约贯通消费，不把“工具执行成功”当成“搜索完整”；不增加语义检索后端或新的 LSP 语言。
- 选择有界现场校验，增加少量 I/O；不增加目录监听或全仓版本索引。fresh 只证明已记录依赖在检查时一致，不能发现新引用者，也不证明查询范围成员未变；负向结论仍需新查询。
- 无全文件哈希的片段和超预算证据保守拒绝作为当前源码，代价是可能需要重取。不会自行扩大预算、补跑工具或触发重规划；大文件未必能在本轮预算内成为 fresh 证据。
- 不覆盖审计历史；失效时放弃历史前缀的单调追加，以防旧摘要再注入。校验不是文件系统原子快照，不承诺校验之后并发写入绝不发生。
- 普通历史 Observation 若没有检索契约，继续遵守原有 blob/生命周期契约，不为其补造源码 freshness。Plan 完成、写权限与副作用恢复仍由既有证据/reducer/journal 门禁决定。

## 实测与证据

Windows / Python 3.13.9，受控 Fake 模型；L2 实际执行读文件、patch_file 和 host pytest。未调用付费模型、未安装依赖、未运行全量测试、未执行远端发布。

按节点去重后的相关结果：**164 passed、0 failed、1 skipped**。跳过项为当前宿主机无法创建 symlink；路径逃逸拒绝另有直接验证，跳过项不算通过。各批次有重叠，不能累加 passed 数；详见 [summary.json](../artifacts/code-exploration-consumption-2026-10-01/summary.json) 与同目录 JUnit XML，可用 `summarize.py` 重算。

| 验证范围 | 证据 |
|---|---|
| 契约持久化、partial/fresh 双轴、未知版本、文件/字节预算、取消/deadline、state_root、hash 复用、scope、blob | `tests/test_code_evidence_consumption.py`；first/fixed/boundaries/scope-final XML |
| 实际 XML/native 请求、工具执行后立即改源码、历史封印、配对、局部片段去重 | projection-final / resume-final / batch-fixed XML |
| checkpoint 评估通过后、模型调用前源码变化，不泄漏旧续接正文 | resume-config-fixed.xml |
| 真实 L2 读→改→host pytest，写操作仅一份成功收据 | l2-fixed / projection-final / scope-final XML |
| 检索、局部关系、LSP 协议、上下文治理、工具/批次、Plan runtime 与已有重规划 | regression.xml、scope-final.xml；只运行相关测试 |

初批失败和精确复验均保留。旧“写后仍展示写前源码”的断言按本轮契约替换；其余失败包括新测试的 mock/Windows 换行/恢复配置假设，以及投影丢失去重前缀和 partial 错误反馈，均有明确修正与复验记录。相关代码 Ruff lint 与 format 检查通过；未据此宣称线上修复率或全量测试通过。

Git 元数据在本会话权限内只读；实现与证据保留在工作区，未创建分支、提交、push 或 PR。
