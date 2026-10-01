# 上下文组装 MVP：统一请求入口与弹性预算

日期：2026-10-02。范围：用户确认的上下文组装增量。基于 [长任务状态 MVP](LONG_TASK_CONTEXT_ACCEPTANCE_2026-10-01.md)，继续沿用既有 PlanSession、Observation、ContextPolicyEngine 和 L0–L5。本文不代表全量测试或在线模型效果验证。

## 已实现范围

1. **统一调用前入口。** XML/native 主循环均调用 `ContextManager.prepare_request()`。工具 schema、完整工具尾部、恢复指令、强制动作和输出上限由调用方显式传入；组装器负责选择、编码、最终预算与 manifest。去掉 Agent 上临时协议预算字段。新入口及辅助模块不引入 `src` 依赖。AgentLoop 中原有修复策略的动态 `src` 导入仍待后续 P3 迁移，本轮没有完成整个 L1/L2 边界拆分。
2. **Plan 路径弹性预算。** 完整规则、角色、目标、约束、当前节点和当前请求先占预算，并预留协议开销。可选内容使用软配额，未使用额度进入回收池，再按当前节点阶段分配；输入总上限和输出上限不因此增大。探索/编辑偏向源码，验证偏向近期工具/验证反馈。已有记忆检索和历史压缩只收集一次候选，借配额不新增 LLM 决策或重新检索。
3. **记录最终请求。** 恢复指令进入受保护请求，native 调用和结果先检查配对，再按完整批次选择。编码后重新计量；Plan 路径若仍溢出，整段舍弃低优先级可选内容，必需内容仍不足则阻断模型调用。最终 manifest 的请求 hash、协议、预算、段 hash、源码/反馈引用及工具 ID 对应发送前请求；checkpoint 保存这些引用与预算说明。回调后、模型调用前再次核对 hash 和权威状态。

```text
权威任务/Plan 与受信任规则
  → 核验状态、源码版本与范围
  → 必需内容 + 显式协议预留
  → 收集已有可选候选
  → 软配额 + 回收池 + 阶段优先级
  → provider 编码 / 完整工具组 / 最终窗口检查
  → 最终 manifest / 发送前复验
```

## 预算与裁剪契约

| 内容 | 预算与保留方式 |
|---|---|
| 目标、全部硬约束、Plan 当前节点、受信任规则和角色 | 完整保留；超限产生 `context_required_over_budget`，不调用模型 |
| 已核验源码候选 | 使用既有任务局部候选；可借其他可选段空闲额度；按完整片段选择 |
| native 工具尾部 | 最多近期三组；每组所有 tool_use/tool_result 配对保留或一起舍弃；大组不能阻止更小近期组入选 |
| XML 近期工具反馈 | 选近期三次结果候选，整条选择；源码正文沿用消费时 freshness 校验 |
| 历史、记忆、知识、工作区摘要 | 软配额与回收池；沿用已有压缩、检索和文本裁剪行为 |
| 最终协议编码开销 | 编码后按现有 tokenizer 重算；Plan 请求只舍弃可选段，再判定必需项是否超限 |

`elastic_budget` 记录池容量、阶段优先级及每段需求、软配额、实际用量、借入、释放、未使用额度和裁剪原因。最终计量包含 XML 连接分隔符，或 native system 与 messages/tools 的 JSON 表达；SDK 包装和服务端真实 token 以 provider usage 为准，不声称本地计量等于服务端精确计量。

源码和工具组的完整保留优先于填满每个 token。贪心装箱可能留下空闲额度，也不保证全局最优。协议开销超限时首版整段舍弃，暂不引入再次打包、全局内容排序或新的摘要模型。源码检查仍为有界 I/O，不能证明文件系统原子快照。

## 借鉴与技术取舍

- **Pi（R-02）：** 采用语义选择与 provider 消息转换分开的边界，主循环消费同一请求准备入口；继续用 Python，不替换运行内核或重写整个 AgentLoop。
- **OpenCode/Pi（R-03）：** 区分工具输出、历史压缩与窗口预留。软配额、回收池和节点优先级是 FixLoop 本轮设计，未照搬上游 token 常量，也不宣称上游提供同样算法。
- **Hermes（R-08/R-09）：** 保留有界记忆和按需历史的方向；本轮复用现有记忆/历史能力，会话 FTS 和经验自动提炼没有新增。
- **范围控制：** 统一入口覆盖 XML/native 主循环。弹性分配仅用于有 Plan 权威状态的路径；普通 L1 固定配额、既有恢复提示契约和预览接口保持。未迁移所有 repair prompt 构建入口，未新增自动重规划、重取或决策提炼。

最终请求 hash 表示发送前输入的身份，恢复依旧由 owner/journal 对账并重建当前请求，不能据此重放旧请求或写入。native hash 包括 system/messages/tools/tool_choice/max_output_tokens；本地 deadline 不作为模型输入。已有知识/记忆 item ID 仍表示检索政策选中候选，可能经过后续文本裁剪；最终段 hash/用量和实际源码、工具引用才是本次发送的直接证据，暂不扩展为所有记忆 item 的完整文本追踪器。

## 验证记录

新增行为测试位于 [test_context_assembly.py](../tests/test_context_assembly.py)，覆盖配额借用、确定性、阶段优先级、大源码借额度、完整工具组、最终编码裁剪、受保护恢复指令、实际 XML/native 请求 hash、checkpoint 和回调篡改阻断。相关回归覆盖上下文预算/压缩、主循环、工具批次、源码失效消费、Plan/L2 真实磁盘工具与 host pytest、公开恢复入口。

最终相关节点去重后 **283 passed、0 failed、0 skipped**，含 26 个新增行为节点。受影响 Python 文件 Ruff check 与 format check 通过，`git diff --check` 通过。未运行全量测试，未调用在线模型，也未推送或创建 PR。

| 报告 | 本批结果 | 覆盖与后续处理 |
|---|---|---|
| `first.xml` | 17 passed | 初次真实请求、语义投影与 native 前缀 |
| `required.xml` | 16 passed / 1 failed | 新增行为；回执夹具失败由 `fixes.xml` 精确复验 |
| `fixes.xml` | 6 passed | 回执夹具、畸形工具组和最终协议裁剪 |
| `core.xml` | 121 passed | 上下文预算、长任务门禁、checkpoint、恢复投影 |
| `runtime.xml` | 120 passed | AgentLoop、native 批次、源码失效、真实 L2 与公开恢复 |
| `source.xml` | 3 passed / 1 failed | 大源码窗口夹具由 `final-manifest.xml` 精确复验 |
| `final-manifest.xml` | 5 passed | 大源码 XML、checkpoint 独立快照与工具组舍弃原因 |
| `source-trim.xml` | 1 failed | 复现嵌套 selection 在最终裁剪后的遗漏 |
| `source-final.xml` | 5 passed | 修复 selection 后仅复验直接受影响的源码消费 |

汇总脚本及机器可读结果见 [summarize.py](../artifacts/context-assembly-mvp-2026-10-02/summarize.py) 与 [summary.json](../artifacts/context-assembly-mvp-2026-10-02/summary.json)。批次有重复节点，不能直接将各批 passed 相加。

本轮早期真实 native 测试夹具未绑定共享 run ID，触发现有回执身份门禁；大源码 XML 夹具初始窗口容不下完整受保护工具说明和片段，调整该夹具预算后精确复验。没有放宽生产身份校验或默认预算。额外裁剪测试发现嵌套 selection 尚列出已舍弃源码，修复后同步选中项和用量。全部原始 JUnit 报告保留在 [本轮 artifacts](../artifacts/context-assembly-mvp-2026-10-02/)；最终按唯一测试节点的最新结果汇总，失败批次不会被删除。
