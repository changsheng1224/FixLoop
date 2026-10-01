# 决策与恢复投影 MVP

日期：2026-10-02。范围：用户确认的显式决策版本化、当前节点消费和恢复重建。承接 [长任务状态](LONG_TASK_CONTEXT_ACCEPTANCE_2026-10-01.md)、[上下文组装](CONTEXT_ASSEMBLY_ACCEPTANCE_2026-10-02.md) 与 [证据消费](EVIDENCE_CONSUMPTION_ACCEPTANCE_2026-10-02.md)，复用 LongTaskState、Plan journal、EvidenceLedger、context manifest 和 checkpoint。

## 本轮实现

| 部分 | 行为与边界 |
|---|---|
| 显式版本记录 | owner API 创建/替换决策；保存 ID、revision、内容、理由、来源、适用节点、Plan ID/version、证据引用及完整 checksum、替代引用、记录 checksum |
| 生命周期 | 新版追加，旧原文保留；历史投影将旧版标为 superseded。当前节点仅消费最新版，证据不可用、证据记录内容变化或 Plan version 变化派生 needs_review |
| 作用域 | 首版每条决策绑定一个节点。当前节点的决策需要复核时阻断模型；其他节点的旧决策不拖住当前探索。不同节点的策略使用新的决策 ID |
| 必需上下文 | 有效决策全文进入压缩保护的 state，其证据并入现有必需摘要/消费记录；发送前复验。超长决策明确超预算，不默默截断或增大默认上限 |
| 恢复 | 对账后从当前 journal/Plan 重建决策；合法新版可以晚于旧 checkpoint。重建失败清除旧活动缓存，返回原因和检查详情 |
| checkpoint | 长任务投影改为深拷贝，最终 manifest 保存决策版本/完整 checksum/检查状态。Plan seal 的长任务快照还须匹配所指 journal 前缀，重新计算摘要不能让伪造快照成为事实 |

`needs_review` 只在消费/恢复时派生，不暗中写入持久状态。重新读取证据不会自动给旧决策换引用，也不会自动确认结论；owner 必须显式复核并提交替代版本。证据或决策摘要都不成为工具收据、Plan 完成条件或重放授权。

## 记录入口与来源

```python
decision = session.record_decision(
    "采用已讨论的实现方式",
    rationale="选择依据",
    source="owner-review:<来源引用>",
    evidence_refs=[evidence_ref],
    node_id="edit",
    expected_plan_version=session.plan.plan_version,
)
revised = session.replace_decision(
    decision["decision_id"], decision["revision"], "复核后的实现方式",
    source="owner-review:<新的来源引用>",
    evidence_refs=[fresh_evidence_ref], node_id="edit",
    expected_plan_version=session.plan.plan_version,
)
```

首次记录和替换均检查 owner 线程、generation fence、Plan version、节点可用性、在途执行、持久状态一致性和证据有效性。证据检查后、提交前再次 fence。替换还比较预期 revision；重复替换、旧 Plan 请求、跨节点改用同一 ID、缺失来源或不可用证据都拒绝写入。首次创建不承诺通用请求幂等，首版未增加新的提交队列。

`source` 是调用方声明的审计定位符，不是用户授权或语义正确性证明。授权来自可信 owner 调用边界，证据从当前 ledger 校验；没有新增消息来源注册表或自动识别用户决策。首版提供 owner API，没有新增自然语言解析、决策工具或 UI。

既有 `key_decisions` 持久容器继续复用，通过 `record_type` 区分三类内容：

- **decision：** 新版正式决策，进入版本链与活动投影。
- **source_review：** Explorer 的主 owner 重读来源事实，记录审计来源与 owner 证据；不自动变成策略决策。真实 L2 调用点已改用此类型。
- **evidence_replacement：** 旧证据与新证据的引用替换关系；不代表决策已重新确认。

历史无版本字典保持审计记录，不自动升级为新版活动决策。普通长任务视图将审计事实与当前活动决策分别输出。用户原始目标和硬约束仍由现有独立字段提供，不由决策投影改写。

## 确认后态与恢复取舍

```text
owner/generation 与资源对账
  → 校验 seal 对应的 journal 前缀
  → 恢复 Plan 执行事实与确认后态
  → 当前节点及最新版决策
  → 证据有效性 + 创建时的证据 checksum + Plan version
  → 必需上下文 / 最终 manifest / 发送前复验
```

确认补丁后，旧输入因源码改变而失效是允许发生的历史事实。此时旧决策保留诊断，过期内容退出活动请求，不因 needs_review 自动阻断确认后态或重放写入。进入下一节点后，只检查该节点适用的决策。尚未确认的在途操作继续沿用 uncertain 门禁，不可用决策刷新绕过。

决策与证据 checksum 固定的是记录版本；校验不证明语义正确。多个不同决策 ID 的语义冲突不自动裁决，Plan version 改变也不自动继承策略。首版仅支持节点作用域，任务级决策、目标/约束来源 turn 注册、自动提炼、历史决策搜索和全部入口迁移后置。多次快照检查增加既有 I/O，不能保证文件系统原子一致性或跨存储原子提交。

## 借鉴记录

- **Pi（R-02/R-03）：** 沿用权威状态、上下文投影与 provider 编码的职责边界。恢复重新生成当前投影，旧 checkpoint 内容作为审计快照；决策版本格式与 needs_review 规则为 FixLoop 本地设计。
- **OpenCode 与恢复边界对照（R-10）：** 复用当前角色/执行边界，继续依靠 FixLoop journal、收据和 generation fence。会话恢复或摘要可读不代表写操作可以重放。
- **Hermes（R-08/R-09）：** 历史检索和经验候选继续后置，本轮没有额外模型调用、自动经验提炼或新数据库。

来源及原有差异分析见 [五维参照](AGENT_DESIGN_REFERENCES_2026-10-01.md)，决策见 ADR-017；未宣称上游具有相同版本链或执行恢复保障。

## 验证记录

相关节点去重后 **220 passed、0 failed、0 skipped**，含 [test_decision_projection.py](../tests/test_decision_projection.py) 的 **30 个新增行为节点**。受影响 11 个 Python 文件 Ruff check/format check 通过；相关已跟踪差异的 diff check、60 个本地文档链接、新增文件空白与 Layer 1 导入边界检查通过（保留 Markdown 两空格换行）。未运行全量测试，未调用在线模型，未推送或创建 PR。

| 报告 | 本批结果 | 验证与处理 |
|---|---|---|
| `first.xml` | 19 passed / 4 failed | 新增行为；只读测量和公开回调夹具由 `fixtures.xml` 精确复验 |
| `core.xml` | 116 passed | 长任务、必需上下文、checkpoint、证据消费、Plan 与重规划 |
| `fixtures.xml` | 4 passed | 只读投影不写 journal，真正发送前的版本替换阻断 |
| `commit.xml` | 5 passed | 坏记录、失效 owner、在途拒绝，以及记录提交后真实进程退出 |
| `runtime.xml` | 73 passed / 1 failed | L2、host pytest、公开恢复/进程崩溃、上下文组装、工具恢复；native 委派预算夹具由后续精确复验 |
| `consumption.xml` | 6 passed / 1 failed | 决策证据进入必需消费、确认补丁后的旧决策退出、重开恢复；native 夹具单改预算仍受原硬顶约束 |
| `native-fixture.xml` | 1 passed | XML/native 委派与来源复核、主 owner 修改、host pytest，未将来源事实提升为决策 |
| `fence.xml` | 9 passed | 检查期间 owner 失效、重复/迟到替换、在途拒绝、真实进程退出复验 |
| `versions.xml` | 30 passed | 固定证据 checksum 后，直接受影响的全部新增决策用例；包括同引用内容变更与显式复核 |

受控 provider 输出与真实磁盘工具/host pytest/进程退出分开评价；这些数字不代表在线模型修复率。所有原始 JUnit 报告保留在 [本轮 artifacts](../artifacts/decision-projection-mvp-2026-10-02/)，[summary.json](../artifacts/decision-projection-mvp-2026-10-02/summary.json) 与 [汇总脚本](../artifacts/decision-projection-mvp-2026-10-02/summarize.py) 按唯一节点最新结果计数，不直接相加重复通过数。

初次新增夹具将 `ask()` 的正常结束 checkpoint 写入误认为只读投影写入，已拆分直接准备请求的测量；回调改用既有 `on_pre_model` 接口。native 委派的完整 schema 加必需状态超过工厂原有 4000 硬顶，夹具显式设置预算与硬顶为 6000，并保持完整协议和 gate；生产默认预算/硬顶均未修改。默认小窗口仍可能因完整必需内容和协议阻断，这一限制没有用静默裁剪掩盖。
