# Code Exploration MVP：演示与面试说明

所有命令从仓库根目录运行。下面的确定性演示使用固定公开输入；它们检查工具契约，不代表真实 Agent 的效果。

## Demo 1：同名符号定位

```powershell
python -m src.eval.code_exploration --suite tests/fixtures/code_exploration/tasks.json --mode lsp --task same_name --deterministic --output eval_results/code_exploration_mvp/demo-same-name
```

预期：`per_task_results.jsonl` 的 `lsp_locations` 包含 `beta.py:1`，不包含 `alpha.py`；`lsp_status` 为 `ok`。trace 保留 `code_lookup` 的命中来源与解析状态。

## Demo 2：测试文件与实现文件的导入关系

```powershell
python -m src.eval.code_exploration --suite tests/fixtures/code_exploration/tasks.json --mode relations --task test_relation --deterministic --output eval_results/code_exploration_mvp/demo-test-relation
```

预期：`relation_claims` 有 `test_imports` 边，从 `test_service.py` 指向 `service.py`，`resolution` 为 `candidate`，并带有 Observation 引用及内容版本。该边只证明导入语法与已观察候选目标，不证明测试覆盖了某个符号。

## Demo 3：修改失效与关闭 LSP 后降级

```powershell
python -m pytest -q tests/test_code_relations.py tests/test_code_lsp.py -k "source_change or successful_unrelated_write or unavailable_server_returns_candidates"
```

预期：3 个测试通过。修改或删除已观察源码以及任意成功写入都会使旧关系视图失效；LSP 不可用时返回有明确原因的文本候选，不声称符号解析成功。

## 面试讲法

- **问题**：文本搜索能找出候选位置，但同名符号、别名和跨文件依赖容易让 Agent 把候选误当成确定关系；重复读取也增加上下文成本。
- **架构**：Layer 1 提供有界文本检索与单服务器 Python LSP。每次命中保存内容版本与真实 Observation ID；任务内关系视图仅连接已观察文件，最多一跳、8 文件。候选源码经哈希校验与上下文预算选择后才进入模型请求。
- **取舍**：AST 的 import 是语法事实；观察到的目标仍标为候选。LSP references 是时点静态证据，不表示运行时调用。没有自动全仓扫描、多跳推断或跨任务图恢复。
- **失败边界**：文件写入、删除、依赖或 Observation 失效会清空整个视图。LSP 缺失时退到受限文本候选；动态调用保留未知状态。Python 以外语言尚未纳入该 MVP。
- **结果来源**：离线 24 次与真实模型 72 次的逐次记录分别见 `offline-final-2026-09-30` 和 `agent-final-2026-09-30`。真实修复任务 9/9 通过针对性测试；定位任务的自动路径提及仅是初筛。模型没有调用 `code_relations`，因此本次不能声称关系视图提升了 Agent 成效。关系组平均耗时高于文本组，样本较小，应作为后续优化线索，而非普遍结论。
