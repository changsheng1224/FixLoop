# FixLoop 代码探索 MVP Spec

日期：2026-09-29。状态：MVP 范围已获用户采纳；本文用于开发，不表示功能已实现。

## 1. 目标与交付

从用户任务中的错误、路径或符号出发，使用文件/文本检索及 Python LSP 找到相关代码，将文件、符号与关系组织为有来源的任务局部关系视图，再通过现有上下文策略选择有限代码片段。

必须交付：

1. 统一检索结果契约与受限的文件读取、搜索。
2. 一种 Python LSP 的定义、引用查询及明确降级。
3. 内存中的任务局部关系视图，关系关联 Observation 与内容版本。
4. 依赖变化后的保守失效，以及恢复后重新探索。
5. 固定任务集、三配置对照记录和一个真实修复闭环。

预计单人 6–10 个有效工作日，包含相关测试和集成返工；环境问题可使工期延长。实现加测试约 1,200–2,000 行是规划区间，不作为验收指标。

## 2. 非目标

- LSP 诊断、重命名、补全、调用层次、其他语言实现。
- 全仓 Code Graph、图数据库、向量检索、自动多跳扩展、图可视化产品。
- 跨任务缓存、目录变更监听、精细增量失效、图持久化恢复。
- 未落盘编辑缓冲区、运行时动态调用解析、测试覆盖率推断。
- 全仓扫描完整性、全局影响分析或检索效果提升幅度保证。
- 重构整个 AgentLoop、ObservationStore 或上下文管理体系。

## 3. 当前基础与架构边界

当前工作树存在未提交变更。开发开始时记录提交 SHA、dirty 状态和相关文件哈希，不覆盖已有改动，不把历史规模或评测数字作为当前指标。

可复用位置：

| 现有模块 | 使用方式 |
|---|---|
| `agent_runtime/tools.py`、`file_listing.py` | 文件、文本工具入口及现有参数契约 |
| `tool_context.py`、`sensitive_paths.py` | 路径解析、边界和敏感内容约束 |
| `tool_result.py`、`tool_executor.py` | 结果规范化、权限、预算、超时链路 |
| `context_runtime.py` | Observation、结构化事实、ContextItem、ContextPolicyEngine |
| `agent_loop.py` | Observation 入库与已执行工具事件 |
| `context_manager.py` | 片段选取及现有上下文预算 |
| `checkpoint.py`、`session_store.py` | 恢复及会话持久化边界 |
| `src/tools/composite.py`、`spec.py`、`manifest.py`、`middleware.py` | 修复 Agent 工具装配、阶段权限 |
| `src/repair/localization/symbol_index.py` | 参考并提取必要 AST 逻辑，不复用仅按仓库路径缓存的索引作为新证据 |

通用代码探索归属 `agent_runtime/code_exploration/`，不得 import `src/`。L2 只负责工具装配、修复域提示和评测。不要迁移完整历史符号索引；按文件提取函数/类/方法和 import 的最小解析器即可。

建议新增文件（允许在职责不变时合并）：

```text
agent_runtime/code_exploration/
  models.py       # 检索、关系、预算数据模型
  io.py           # 受限读取/搜索、内容指纹
  lsp.py          # stdio 客户端、位置转换、文档同步
  service.py      # 查询、降级、作用域和资源生命周期
  relations.py    # AST、节点/边、证据绑定、失效
  context.py      # ContextItem 适配
```

## 4. 工具与配置

保留现有 `list_files`、`grep`、`read_file` 的名称及必需参数，内部使用统一结构化结果，模型可见文本保持可读。修改返回对象前适配 `inspect_file` 等当前字符串调用方，禁止让新对象被直接拼成 Python repr。

新增两个模型工具：

```text
code_lookup(path, line, column, operation="definition"|"references", max_results?)
code_relations(max_files?, top_k?, token_budget?)
```

- `code_lookup` 的位置为 1 起始行、1 起始 Unicode 字符列；参数必须指向仓库内已落盘文件。引用默认排除声明。
- `code_relations` 只组织本次任务已经探索的命中及最多一跳的已观察关系；不自动发起新的 LSP 查询或多跳搜索。
- Agent 根据任务自行选择查询锚点和下一次探索，不增加一个 LLM 规划 Agent。
- 两个工具是只读工具，但 `code_lookup` 会启动受控进程。进入原有工具注册、权限、执行和预算链路，不绕开阶段门禁。
- 启用功能时，两工具在该次运行的 canonical registry 中稳定存在；不同阶段只改变权限，不动态改 schema。
- 使用受信任运行配置 `code_exploration.mode = text | lsp | relations`。默认 `text`；`lsp` 开启语义查询，`relations` 额外开启关系视图与片段选取。禁用模式查询新增能力时明确返回 `disabled`。
- 服务器命令是受信任配置中的 argv 列表，默认适配器为 `pylsp`。模型参数中不接受命令、环境或插件配置。

## 5. 统一检索契约

所有路径用仓库相对 POSIX 路径表示。内部范围统一为 1 起始行/列、终点不包含；文本行命中没有精确列时列可为空。每个范围记录是否精确，不能虚构列号。

```text
RetrievalHit:
  hit_id, path, range?, kind, summary
  source: filesystem | text | ast | lsp
  resolution: syntactic | resolved_by_lsp | candidate | external | unresolved
  content_hash?                 # 完整文件内容哈希，不能填成工具版本或片段哈希
  excerpt_hash?                 # 片段摘要哈希，明确区别于文件版本
  server_id?, observed_at

RetrievalResult:
  schema_version="1", query_id, query_type
  execution: ok | unavailable | unsupported | timeout | cancelled | rejected | error
  completeness: complete_in_scope | partial | unknown
  hits[], scanned_scope, observed_at
  truncation_reasons[], degradation_reason?
  dependency_versions: {relative_path: content_hash}
  budget_used, duration_ms
```

空数组可以是成功完成查询，也可以是超时产生的部分结果，必须结合状态解释。`complete_in_scope` 只说明既定范围内查询完成；LSP 正常响应不等于完整认识仓库或运行时行为，LSP 结果默认 `unknown`。

降级成功：`execution=ok`、source 标为降级来源，并保留 `degradation_reason`。拒绝敏感路径时不返回任何部分源码。命中数只是返回命中数；未扫描完整范围时不输出精确总数。

`code_lookup` 不接受符号名替代精确位置；降级时从校验后的源文件位置提取标识符，再做受限候选搜索。位置没有标识符时返回明确原因，不搜索整个任务字符串。`code_relations` 没有当前证据时返回空视图，不隐式搜索全仓。

结构通过 `ToolResult.metadata` 进入执行器和 Observation（例如 `retrieval_result`、`structured_facts`、`source_dependencies`）；不得只放在当前执行器未透传的 `data` 字段。`ToolResult` 顶层状态继续使用既有枚举，不把检索枚举直接塞入顶层。

## 6. 受限读取、搜索与安全

预算由受信任配置提供；工具参数只能降低上限。默认值是实现起点，P0 固定后对照期间不变：

| 项目 | 默认上限 |
|---|---:|
| 范围读取实际扫描 | 256 KiB / 调用，包括跳过起始行的字节 |
| 范围读取返回 | 200 行、32 KiB |
| 文本查询候选文件 | 300 个 |
| 文本查询累计读取 | 2 MiB |
| 单文件 AST/LSP 内容 | 256 KiB |
| 检索返回命中 | 50 条 |
| 模型可见工具内容 | 64 KiB，且服从更小的现有运行时上限 |
| LSP 初始化 / 单请求 | 5 秒 / 3 秒 |
| 单个 LSP 消息体 | 1 MiB，读消息体前检查 Content-Length |
| 关系视图 | 8 文件、64 节点、128 边、最多一跳 |
| 上下文片段 | Top-K=6、1,500 token，服从剩余上下文预算 |

实现要求：

- 范围读取采用有界字节块与增量解码，不能 `read_text().splitlines()`，也不能用无限制 `readline()` 读入超长单行。
- 起始行很深且扫描预算耗尽时，返回 partial 与 `scan_bytes` 原因，不声称文件行数不足。总行数未知时不为了标题再扫描到 EOF。
- AST/LSP 只接收大小在上限内的文件。大文件仍可范围读取，但不生成可复用的完整文件版本或 AST 关系。
- 内容哈希使用受限流式读取。Observation 指纹计算不能再次无限制 `read_bytes()`；新增链路必须复用经校验的依赖版本。
- 文本检索先得到受限候选范围，再搜索。rg 使用有界输出读取、deadline 和取消清理；禁止 `capture_output=True` 收集无限输出后截断。Python 降级同样受文件、字节和时间预算限制。
- 本次搜索达到候选或读取上限即返回 partial；不要求发现上限之外的文件。
- 每个候选文件、AST 输入、片段读取、指纹读取都经过 `ToolContext.resolve` 和敏感路径检查；只检查搜索根目录不够。
- LSP 外部 URI 只能生成 `external` 标记，默认去除可能泄漏用户目录的绝对路径；不得据此读取文件。拒绝非 file URI、畸形 URI和工作区逃逸。

## 7. Python LSP

首期使用 `python-lsp-server` 的基础安装，提供 `pylsp` 命令，定义与引用由其 Jedi 能力提供。安装由开发者显式进行，Agent 不自动安装。真实验证时记录 Python、服务器及 Jedi 的实际版本；不声称尚未验证的版本组合受支持。

- 进程运行于开发者指定的独立可信环境，使用显式配置，禁用不需要的 lint/format 插件及仓库配置源；不加载仓库声明的插件或任意程序。
- 正确实现 LSP Content-Length framing。现有 MCP stdio 是换行 JSON 协议，不能直接作为 LSP transport 使用。
- 生命周期：initialize → capabilities → initialized → didOpen/didChange → request → shutdown/exit。
- MVP 顺序处理请求，不做并行请求池。仍需分辨通知、响应和服务器请求；未知服务器请求明确答复不支持，不能阻塞读循环。
- 位置转换集中实现；处理 LSP 协商位置编码与 UTF-16 代理对，不能只加减 1。URI 转换测试覆盖 Windows 盘符、空格和非 ASCII 路径。
- 文档版本使用客户端递增整数，另记录内容哈希；两者不得混用。
- 查询前同步当前落盘内容。查询后复查锚点及将要使用的结果文件，变化则丢弃结果、最多重试一次；仍变化返回 partial/error 并说明原因。
- LSP 不提供所有依赖的快照事务，结果只作为指定时点、指定服务器报告的静态证据。任何文件写入后重新查询 LSP 关系，不复用旧的全局引用结论。
- 返回 Location 与 LocationLink 时统一规范化；references 不能当成运行时调用边。
- 不可用、不支持、初始化失败、超时或协议错误时降级为受限文本查询/单文件 AST；候选标记明确。取消、权限拒绝、预算耗尽不通过降级绕过。
- 超时/取消后有界关闭进程；当前任务中不自动循环重启。任务结束关闭句柄和进程，后续任务可按需重新启动。

## 8. 任务局部关系视图

作用域是 workspace_id + session_id + task_id + run_id + exploration_epoch。run 与 task 不能默认为同一个；task_id 来自当前任务状态，无 ID 时在任务开始创建并维持到任务结束。

exploration_epoch 是探索代次，不是文件版本。任务开始、写入导致整体失效、依赖校验失败及恢复时生成新代次。代次内保留查询时点，不能把相同代次理解为全仓没有外部修改；外部依赖变更仍通过使用前内容校验发现。

内存对象记录 view_revision、observation_refs、dependency_versions、covered_files、coverage 和 observed_at。只保存节点/边与引用，不复制原始源码。

节点：文件、符号；可选外部/未解析引用。符号身份由路径、限定名、类型和定义范围组成，同名符号不能合并。内容哈希作为版本，变化后整个视图失效并重建。

边：

| 类型 | 语义 |
|---|---|
| contains | AST 显示文件包含符号 |
| imports | AST 显示导入语句；模块解析成功与否另行记录 |
| references | LSP 报告引用位置，或文本候选关联 |
| test_imports | 测试文件导入实现模块；不表示测试覆盖或验证符号 |

定义以符号节点的定义位置表示，无需另造一个 definition 边。每条边含 source、resolution、Observation ID、证据路径/范围、依赖内容版本、observed_at。AST 发现 import 只确认语法关系，不能自动确认目标模块；相对导入和别名按证据解析，无法解析时保留候选。

一跳指当前种子与直接相邻已观察节点，不递归跟随新增邻居。不进行没有证据支持的连边。返回每个纳入文件的 seed → relation → target 路径及理由，超限返回 covered_files 与截断原因。

### Observation 绑定顺序

查询工具先返回结构化 hits/facts；AgentLoop 沿既有路径只入库一次。拿到真实 Observation ID 后，调用轻量 `record_observation(id, facts, dependency_versions)` 更新内存视图；禁止工具提前伪造 ID或再保存一份原始结果。

`source_dependencies` 必须传到 ObservationStore，而不是仅使用 changed_files。文件写入依旧使原有 Observation 失效，另通知探索服务清空视图及语义查询状态。

### 上下文接入

`code_relations` 返回关系摘要与候选片段描述；片段经预算读取、有效性校验后转成 `ContextItem(kind="source")`，引用 Observation 与内容版本。由现有 `ContextPolicyEngine.select_with_result()` 选取，接入 ContextManager 已有 source/observation 通道。

记录 selected/dropped 及原因，避免同时在工具历史和 source 通道重复注入同一片段。图结构本身不整体注入。接入完成的证明是模型请求中的上下文实际包含选中片段及引用，不能仅证明工具返回了 JSON。

## 9. 复用、失效与恢复的有限承诺

- 只复用任务内的单文件 AST和完整小文件片段。复用前重新校验依赖内容哈希及路径策略，按预算计费；预算不足则不复用。
- 目录/glob/文本搜索及 LSP 引用结果不跨探索轮缓存，不缓存“没有找到”结论。新文件通过新的查询发现，不保证任意外部新增文件立即触发事件。
- 关系视图是已观察事实的组织；重用它不表示重新检查了全仓关系。响应必须保留 observed_at、覆盖范围和当前已验证的依赖。
- 任意成功文件写入清空任务视图。再次使用时发现依赖改变、删除、重命名、Observation 缺失/stale/checksum 不符，也清空视图，不做逐边修补。
- LSP 结果是时点证据。跨探索轮需要当前语义关系时重新查询，不能用只检查命中文件哈希替代全局引用校验。
- 探索服务对象和 LSP 进程不放入持久化 session。checkpoint 不新增图 schema；session 序列化不得带入内存服务或可复用视图。
- 恢复时创建新 exploration_epoch，丢弃 source candidates/视图并重新查询。既有 Observation 可保留作历史，但旧探索证据在重新校验前不能自动作为当前 source 上下文；工具步骤恢复也执行该规则。

## 10. 事件

扩展现有 Canonical Trace，不另建日志系统：query_start/query_end、lsp_degraded、relation_view_built、exploration_invalidated、exploration_reset_on_resume。

携带 task/run/query/epoch、查询类型、返回命中数、覆盖范围计数、partial 原因、预算、耗时、server_id、Observation refs；默认不记录完整源码、原始搜索文本或外部绝对路径。查询可用哈希关联。事件失败不得改变工具结论。

## 11. 验收矩阵

| ID | 场景 | 必须观察到的结果 |
|---|---|---|
| A1 | 错误路径和符号定位 | 找到定义、相关位置，形成有限片段上下文 |
| A2 | 别名/同名符号 | 不合并不同符号；LSP 与文本候选可区分 |
| A3 | 测试导入、相对导入 | 边有来源和范围；不声称覆盖；未解析关系明确 |
| A4 | 动态调用/未解析模块 | 不产生确定调用边或不存在结论 |
| A5 | 文件修改/删除/重命名 | 旧视图失效；重查重建；不会注入旧片段 |
| A6 | 新增引用者 | 新一轮搜索/引用查询能发现；不承诺即时监听 |
| A7 | LSP 缺失/失败/超时 | 明确原因与候选降级；取消不会触发额外探索 |
| A8 | 大文件、超长行、深行号、超多命中 | 实际读取/输出受限；partial 原因准确 |
| A9 | 非 BMP 字符、Windows URI | 行列转换正确；外部/敏感/逃逸路径无源码泄漏 |
| A10 | checkpoint 恢复 | 新 epoch、无旧视图/进程、旧 source 不自动进入请求 |
| A11 | 真实修复 | 检索证据进入 Agent 请求，产出修改并通过任务测试 |

A1–A11 是确定性测试与集成测试要求，不要求每项都单独跑模型实验。真实 pylsp 验证必须实际执行，未安装时可跳过本地集成测试但交付不能把 skip 当通过。

## 12. 固定任务与对照

固定 8 个小型 Python 仓库任务：错误到定义、同名符号、别名引用、跨文件导入、测试关联、动态/未知目标、重复探索、一次修复。优先从历史问题抽取通用结构，用合成 fixture 固定预期；保留至少一个真实项目片段任务并注明来源。

任务数据分为公开输入和独立 oracle，最小格式如下：

```text
tasks.json:
  schema_version, tasks[]
  task: id, category, fixture_dir, prompt, entry_paths[], budget_profile
        source_attribution?, verification_argv?   # 固定可信 fixture 测试命令

oracles.json:
  task_id: accepted_definitions[{path, qualified_name, range}]
           relevant_files[], required_relations[], forbidden_claims[]
           success_predicate, expected_test_exit?

per_task_results.jsonl:
  task_id, mode, repetition, runtime_versions, fixture_hash, config_hash
  execution_status, predicted_locations[], selected_files[], relation_claims[]
  correctness, metrics, trace_ref, evidence_refs[], verification_result?
```

模型只看到 prompt 和正常仓库内容，不看到 oracle。任务涉及变更时在确定性测试中由测试驱动器执行，不通过模型工具参数接收任意测试命令。结构化预测与证据由 trace/工具记录提取；无法解析最终回答时记 `unscorable`，不能当成成功或从耗时统计中静默删除。定位成功要求命中 oracle 中允许的路径与定义范围；关系正确性按预定义谓词裁决，不能由另一个未校准的 LLM 随意打分。

测试 oracle 仅供评测器裁决，不注入 Agent。运行配置：T=文件/文本；L=T+LSP；R=L+关系视图及上下文选取。

- 三组共用改造后的有界文本后端。P0 另保存原始版本快照，不把 I/O 改造收益混算为 LSP 或视图收益。
- 固定仓库快照、模型、prompt 基础版本、任务输入、预算；工具差异只来自配置，记录实际工具 schema。每次新 session/task 和干净工作树。
- 每个模型任务每配置至少 3 次，共 72 次；先跑一个 smoke 任务。预算不足时减少任务数量/重复数必须明确标记为不完整实验，不伪称完整验收。
- 冷启动计入 LSP 初始化；热启动只复用同任务进程，仍重新同步/查询，单独统计。没有先前结果时冷启动与热启动不能混为一列。
- 指标：定位正确性、错误确定关系、所选上下文无关文件数、重复读取、模型工具调用数、内部后端请求数、input/output token、总耗时与 LSP 初始化耗时；修复任务另记测试是否通过。
- 重复读取按同文件同版本的重叠范围定义，区分模型请求与实际 I/O；正确定位和相关文件集合由任务 oracle 预先定义。
- 保存任务输入、模式、版本、seed/采样配置、trace、证据引用、选中片段、结果及逐次指标。报告中同时展示失败与成本增长，不预设改善幅度。

交付至少三个演示：同名符号定位、修改影响证据、文件变化失效与 LSP 降级。不以功能名代替效果结论。

## 13. 开发与验证纪律

按配套 plan 顺序开发。开发前说明该阶段目标和涉及模块；本 spec 的采纳不是自动授权安装依赖、真实付费模型调用、push 或合并。

优先相关测试，然后检查改动范围及相关包的 lint/format。遵守 `CLAUDE.md`：全量测试仅在用户显式授权时运行；未运行不得声称通过。本文不会自行开启全量测试、网络安装或远端操作。

## 14. 技术参考

- [python-lsp-server 官方文档](https://github.com/python-lsp/python-lsp-server)：基础安装、定义/引用与配置来源。
- [LSP 协议](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/)：消息 framing、生命周期与位置规范。
- [位置编码类型](https://github.com/microsoft/vscode-languageserver-node/blob/main/types/src/main.ts)：编码协商与 UTF-16 位置。
