# S4：历史压缩契约完善

日期：2026-10-13。分支：`M1/D4/history-compression-s4`。这是本地开发验收，不包含远端发布或全量测试。

## 修复的问题

原 L5 全量摘要要求 JSON，增量摘要要求自由文本；输入逐条截取少量字符，输出硬切 300 字符；失败按最近 8 条消息裁剪，可能拆开工具调用与结果。ContextManager 的显示和 section 填充还会再次截断已经生成的摘要。

S4 把格式、选择、缩减与失败处理连接到最终组装边界。摘要仍是可选历史辅助，权威任务状态继续独立装配。

## 模块与契约

| 模块 | 实现 |
| --- | --- |
| `agent_runtime/history_summary.py` | 同一 schema 校验、完整交互分组、输入 token 装箱、输出完整条目缩减、协议身份序列化 |
| `agent_runtime/compression_pipeline.py` | L0–L5 消费统一分组；全量/增量摘要替换；来源无关的输出校验；预算内回退；调用前保护区检查 |
| `agent_runtime/context_manager.py` | 显示阶段不二次截断；普通布局与 Plan 弹性布局按完整单位选择；封印前缀整体处理 |

全量和增量摘要均包含且只能包含下列字段：

```json
{
  "confirmed_facts": [],
  "hypotheses": [],
  "rejected_hypotheses": [],
  "files_changed": [],
  "verification": {"status": "unknown", "details": []},
  "next_action": "",
  "evidence_refs": []
}
```

列表项必须是非空字符串，每个列表最多 128 项；`next_action` 是字符串；verification 状态限定为 unknown/not_run/passed/failed。拒绝自由文本、残缺 JSON、重复键、额外字段、错误类型及未提供的证据引用。此前摘要中的引用允许作为历史指针继续提供，不代表当前 freshness 已通过。

输入上限取 6000 token 与 S1 单次输入容量的最小值，包括指令、历史和 JSON 开销。按完整用户交互组选择，优先失败信息，再优先近期；无法装入的整组省略并报告数量。不会截取一条消息前 150 字符后假装覆盖全文。

摘要 JSON 上限取 512 token 与 history window 三分之一的最小值。按完整条目删除并重新序列化，优先减少假设、已排除假设、文件记录和事实，之后减少 next_action、验证详情、引用；schema 和验证状态始终保留。最小结构也装不下时走确定性回退。遗漏交互和摘要条目由宿主标记，不能由模型假装没有遗漏。

make_summarizer 沿用 S1 complete_text：共享累计账本、输出预留、取消、任务 deadline，摘要调用另设 30 秒超时。无 usage 正常返回记 estimated；中断/异常保留 unknown 预约；不会退还未知消耗。有效 usage 使用实际计量。没有新增摘要格式修复或自动重试调用。

## 完整交互与失败边界

- 支持本项目 XML 历史记录、Anthropic tool_use/tool_result，以及 OpenAI tool_calls/tool_call_id；native ID 不匹配、重复或悬空时明确阻断。
- L0 不会单独删除关联调用的结果，也不会因 assistant 空正文删掉 native call。native 历史通过 JSON 保留 ID、参数和结果，不只显示 content。
- L2/L4 可以折叠整个旧交互；L5 和最终 history section 只能整组选取或省略。既有 L1 对普通工具正文的 token 缩减继续存在，原始 Observation 与 canonical 历史不改写。
- 无摘要服务、结构无效、输入/输出容量不足、Provider 异常/超时和累计额度不足时，保留首个用户交互、当前 turn 和受保护状态，再按近期顺序选择完整交互，默认最多 8 组；必需组可以超过该组数上限。
- 保护区本身装不进 history window 时，在辅助调用前抛出 `history_protected_over_budget`。错误会进入已有上下文阻断路径，不提交残缺请求。
- 取消直接传播；不会被当作普通摘要失败后继续执行。
- section 预算仍可省略整个可选历史组/摘要。遗漏提示也装不下时历史段为空，遗漏数量仍在 compression_pipeline 的组装元数据中。Plan 必需任务、约束、决策和当前必需证据仍由 S1/S2 required sections 保证。

## 增量摘要与封印前缀

同一次未封印投影把所有既有合法摘要与新增选中交互交给相同 schema 的合并请求，生成的新块替换旧块；失败则移除不可用摘要，显式报告其省略。

已封印前缀保持既有单调追加机制。前缀已经含摘要时，新增尾部超限只做确定性裁减，不生成第二个摘要；下一 run 从 canonical 历史重新投影。section 装不下封印段时只能整体省略，不能切开它或改写它。这意味着单 run 中已封印摘要不会随每步重新刷新；当前权威状态仍独立刷新，不依赖摘要的 verification/next_action。

## 缓存与状态边界

没有修改 `_summary_cache_key`、磁盘存储、失效策略、Provider cache_control、prefix hash 或缓存指标实现。缓存原文和新模型输出使用同一个 schema 校验器；不符合结构时记录失败原因并回退，不修改/删除缓存记录，也不为修复缓存文本额外调用模型。

压缩输出不调用 S3 提取/提交入口，不提交 hard_constraints、决策或执行收据，不从摘要重建活动状态。结构校验区分字段和引用，不证明摘要事实正确、语义无遗漏，也不证明模型遵循约束。摘要引用必须按既有 EvidenceLedger 的当前用途重新核验，才能成为后续执行证据。

## 相关验证

- 压缩、TaskState 与尾部保护首批：54 项通过。
- 上下文组装、必需状态、投影、section 顺序、模型预算、tier 与消息封印的相关批次：207 项；4 项旧预期/新测试预期修正后按 nodeid 通过。
- 新增 XML/native 实际请求检查：摘要与权威状态隔离；压缩调用不写 Plan journal；原 canonical 历史保持完整。
- 新增无 usage 正常/超时、预算恢复、已封印单摘要和 OpenAI 参数格式化检查，均通过。
- 后续分组/格式化调整对应的压缩边界复验：85 项通过。
- 修改 Python 文件执行 Ruff check、format check；执行 git diff --check。

没有运行全量测试，没有付费模型调用。这些是受控模型的结构/调用契约测试，不作为语义摘要准确率或在线修复率证明。P5 主路径说明收敛仍待实施。
