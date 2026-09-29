# FixLoop 代码探索 MVP 开发计划

日期：2026-09-29。依赖：[MVP Spec](../specs/2026-09-29-code-exploration-mvp.md)。未开始实现。

## 执行方式

先完成 P0，再依序 P1 → P2 → P3 → P4。阶段边界用于验收和控制范围，不要求新建用户任务或多 Agent 并行工作。

当前工作树有大量未提交修改，不能 checkout、reset、stash 或覆盖它们来得到干净基线。记录当前状态，后续实现的隔离方式先根据已有工作确定；单纯从 HEAD 创建 worktree 不会带入这些改动。

每阶段只处理该阶段需求，保持 L1 不 import L2。Git 分支与 PR 按 `CLAUDE.md`；不自动 commit/push/创建或合并 PR。全量测试与真实模型实验各自遵守授权范围。

## P0：固定范围与基线（1 天）

目标：把效果验证前置，并确认真实 LSP 环境，避免全部实现完成后才发现无法验证收益。

任务：

1. 记录提交、dirty 状态、相关源码哈希、Python 与测试工具版本；只记录必要文件，排除 artifacts/logs/快照大目录。
2. 新增 `tests/fixtures/code_exploration/`：按 spec §12 定义 `tasks.json` 与独立 `oracles.json`，配置 8 个任务、相关文件、正确符号位置、候选关系和修复测试。
3. 在 `src/eval/code_exploration.py` 或同职责评测入口建立任务加载和结果格式；建立每次独立临时仓库的运行方式。
4. 保存当前文件/文本实现的基线记录；明确这与 P4 共用改造后文本后端的 T 组不同。
5. 确认 pylsp 可执行程序来源。需要安装时显式处理依赖授权，不让模型工具安装；基础安装使用独立可信环境。
6. 固定 spec 中预算默认值、服务器实际版本与实验配置。评测对照期间不得按某个任务调整预算。

验收：任务输入、oracle、结果 schema 可读取；一个无需模型的任务能输出定位结果和调用记录。真实模型运行如尚未授权，记录 pending，不阻塞后续确定性测试。

预计新增：fixture/评测代码 100–180 行，任务文件另计。

## P1：统一契约和受限 I/O（1–2 天）

主要模块：`code_exploration/models.py`、`io.py`、`tools.py`、`file_listing.py`、`tool_result.py`，必要时适配 `src/tools/composite.py`。

任务：

1. 定义 RetrievalHit/Result、范围和预算模型；实现模型可见 renderer。
2. 范围读取使用有界字节块/增量解码，限制跳行成本、长行和返回内容；大文件允许有限范围读取。
3. 文件候选枚举与 rg/Python 搜索受文件数、读取、输出和 deadline 约束。每个文件单独做路径/敏感检查。
4. 返回结构化 metadata、partial/degradation 和覆盖范围；保留旧工具必需参数和字符串组合工具的可读行为。
5. 新增链路的内容哈希和 Observation 入库不能通过隐式整文件读取绕开预算；完整 hash 与片段 hash 分开。

相关测试：现有 `test_tools.py`、`test_file_listing.py`、`test_path_safety.py`、`test_tool_runtime_contracts.py`；新增 `test_code_retrieval.py`、`test_code_retrieval_limits.py`。

测试重点：用实际读取计数器证明限额，而非只检查输出长度；覆盖深行号、超长行、无命中、超时部分结果、rg 降级、单文件搜索、二进制、敏感子文件及 symlink 逃逸。

验收：A8/A9 路径部分通过，T 模式正常工作，原有相关工具契约不破坏。

预计实现 180–280 行，测试 100–160 行。

## P2：Python LSP 定义/引用及降级（2–3 天）

主要模块：`code_exploration/lsp.py`、`service.py`、`config.py`/现有配置加载、`tool_context.py`、工具注册和 L2 spec/manifest/权限。

任务：

1. 受信任 argv 与显式 pylsp 配置；只启用所需能力，不加载仓库插件/命令。
2. 实现有界 Content-Length framing、初始化、能力探测、文档打开/变更、顺序请求、通知分流、服务器请求响应及资源清理。
3. 集中处理范围/字符编码/URI转换，包括 UTF-16 非 BMP 字符和 Windows 路径。
4. 注册 `code_lookup`，支持 definition/references 和 Location/LocationLink 规范化。
5. 同步落盘版本，结果使用前校验；遇到变化最多重试一次。
6. 不可用等条件返回文本/AST 候选并说明原因；取消/权限/预算不能通过降级绕过。
7. 模式在运行开始固定；canonical 工具列表、prompt 工具签名、phase 权限与工具 manifest 同步更新。

相关测试：新增 `test_code_lsp.py`、`test_code_lsp_integration.py`；已有 `test_tool_executor.py`、`test_tools_manifest.py`、`test_repair_tool_schema_stable.py`、`test_phase_b_tools.py` 中相关用例。

测试重点：假 server 注入超长消息、错 ID、通知穿插、超时、取消；真实 pylsp 测试定义/别名引用/同名符号；配置不能被工具参数覆盖；外部 URI 不触发读取。

验收：A1/A2/A7/A9 语义部分通过；至少一次真实服务器结果记录；任务结束后无遗留进程。真实集成跳过不算此阶段完成。

预计实现 250–400 行，测试 120–200 行。

## P3：任务关系视图与上下文闭环（1–2 天）

主要模块：`code_exploration/relations.py`、`context.py`、`service.py`、`agent_loop.py`、`context_runtime.py`、`context_manager.py`，任务结束及 resume 生命周期入口。

任务：

1. 按文件提取限定名定义与 import；实现 contains/imports/references/test_imports，分离语法、LSP 解析和候选。
2. Observation 入库后绑定真实 ID；透传 source_dependencies，避免读查询依赖被 changed_files 掩盖。原始结果只入库一次。
3. 新增 `code_relations`：从当前任务证据建立内存视图，最多一跳、8 文件；返回纳入理由与覆盖/截断。
4. 单文件 AST/片段缓存校验内容 hash；不缓存目录查询、全局引用或无命中结论。
5. 片段适配 ContextItem，交由现有 ContextPolicyEngine 选择；纳入后续模型请求，记录 selected/dropped，避免历史重复注入。
6. 任意写入保守清空视图；依赖变化/删除/重命名或 Observation 失效时同样处理。
7. 服务保持内存态。检查 session 序列化无服务/视图，resume 创建新 epoch 并清空未校验的旧 source 候选，包含工具步骤恢复。
8. 接入最少必要事件与关联 ID，不在默认日志中写源码。

相关测试：新增 `test_code_relations.py`、`test_code_exploration_context.py`、`test_code_exploration_resume.py`；现有 `test_context_manager.py`、`test_observation_store_governance.py`、`test_checkpoint_resume.py`、`test_strong_step_resume.py`、`test_session_bak.py` 中相关用例。

验收：A3–A6/A10 通过；捕获模型请求确认被选片段实际进入上下文、旧片段不会继续进入；不存在 L1→L2 import。

预计实现 180–280 行，测试 120–200 行。

## P4：固定对照、修复与交付（1–2 天）

主要模块：P0 评测入口、任务集、Demo 与报告文档；不借评测阶段扩展主功能。

任务：

1. 先跑确定性验收矩阵与真实 LSP 集成，修正契约问题后冻结实现及配置。
2. 运行 T/L/R 三组，每组每任务 3 次，记录实际工具 schema、模型、仓库/源码版本和预算。真实模型调用需要明确授权；未授权只完成离线验证及待执行命令。
3. 每次模型实验恢复干净任务仓库、创建独立 session/task。恢复/变更测试另外执行，不污染定位对照。
4. 输出逐次记录、分任务汇总、冷启动/热启动成本及失败分析；不能只报告总体均值或最优一次。
5. 至少一个真实模型修复任务通过其针对性测试，关联修改与实际检索证据。
6. 编写三个 Demo 的命令和预期输出：同名符号定位、实现/测试导入关系、修改失效与关闭 LSP 降级。
7. 基于实测写面试说明：解决的问题、架构取舍、失败边界、结果与下一步；不使用未经测量的提升百分比。

建议评测接口（新增后方可使用）：

```text
python -m src.eval.code_exploration --suite tests/fixtures/code_exploration/tasks.json --mode text --deterministic --output <output-dir>
python -m src.eval.code_exploration --suite tests/fixtures/code_exploration/tasks.json --mode lsp --deterministic --output <output-dir>
python -m src.eval.code_exploration --suite tests/fixtures/code_exploration/tasks.json --mode relations --deterministic --output <output-dir>
```

模型模式另提供 `--agent --repetitions 3`；不能用 deterministic/FakeClient 数据证明真实 Agent 效果。

交付目录建议：

```text
eval_results/code_exploration_mvp/<run-id>/
  run_manifest.json
  per_task_results.jsonl
  traces/
  evidence/
  report.md
```

预计评测/演示代码 80–160 行，报告及 fixture 另计。

## 完成门禁

- Spec A1–A11 具备可检查记录，真实服务器与真实修复没有被 mock/skip 替代。
- 两个新增工具经过权限、预算、结果规范化与 Observation 链路。
- 数据说明查询覆盖范围，候选与静态关系不会被包装成全局调用/测试覆盖结论。
- 源码读取、输出采集及内容版本校验均受预算约束。
- 修改/恢复后不存在自动注入的过期 source 候选，LSP 进程不持久化。
- 相关测试和 lint/format 完成；全量测试只有获授权后执行。
- 三配置真实对照完成才宣称效果验证完成；未授权/失败的项单列 pending/failed，禁止用文档补齐执行证据。

## 控制范围与决策点

P2 完成后检查定义/引用是否在固定任务中提供区分能力；若环境成本超过计划，先解决单服务器可复现性，不追加另一服务器。

P3 不加入自动多跳、跨任务复用、图恢复或新排序模型。P4 即使发现视图未带来成本/正确率收益，也如实交付；后续投入需由数据支持。

每阶段保持可演示、可验证，不以完成大量内部抽象作为阶段验收。
