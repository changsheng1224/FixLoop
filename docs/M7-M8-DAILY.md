# M7-M8 每日开发计划（Week 13-16）

> 每天约 4-6 小时有效编码时间。⚡ 核心任务必须完成，🔧 辅助任务可弹性。Day 编号接续 M1-M6。

---

## M7：评测体系 + 消融实验 + CI 回归门禁（Week 13-14）

**目标：用数据证明 Multi-Agent 真分工比 Single-Agent 更有效。90 次实验，Fix Rate 对比 +30pp。**

---

### Day 31（周一）：10 个评测 Case 构建（上）

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 创建 `src/eval/cases/` 目录结构。设计 Case 覆盖矩阵——5 种错误类型 × 2-3 难度级，共 10 个 Case。每个 Case 目录结构：`issue.txt`（错误描述/堆栈）、`expected_patch.diff`（人工标注正确修复）、`min_lines.txt`（最小必要修改行数）、`metadata.yaml`（language/issue_type/difficulty/estimated_duration）、`repo/`（含 bug 的微型 Python 项目） | 10 个空 Case 目录就绪，矩阵表完成 |
| 10:00-12:00 | ⚡ 构建 Case 001-003（TypeError 类，简单→中等难度）：每个 Case 的 repo 含 1-2 个源文件 + 1 个测试文件。手动引入 bug（参数类型未转换、返回值类型错误、None 未判断）。写 `issue.txt`（模拟真实 GitHub Issue 或 CI 日志格式）。人工编写 `expected_patch.diff`（正确的最小修复）并标注 `min_lines.txt` | 3 个 TypeError Case 完成，可独立 `pytest` 复现 |
| 12:00-12:30 | 🔧 验证：在每个 Case 的 `repo/` 中运行 `pytest` → 确认有测试失败。运行 `expected_patch.diff` 中的修复 → 确认测试全绿 | 3 个 Case 的 bug 和修复均可复现 |
| 14:00-15:30 | ⚡ 构建 Case 004-006（ImportError + 逻辑错误，中等难度）：Case 004 `ImportError`（缺少 `__init__.py` 或模块路径错误）；Case 005 `ImportError`（依赖版本不匹配提示）；Case 006 逻辑错误（off-by-one 边界条件）。每个 Case 含 2-3 个源文件 + 测试 | 3 个 Case 完成并验证可复现 |
| 15:30-16:30 | ⚡ 构建 Case 007-008（AttributeError + 逻辑错误，中等→困难）：Case 007 `AttributeError`（访问 None 对象属性）；Case 008 逻辑错误（算法逻辑缺陷，需 3-hop 调用链分析） | 2 个 Case 完成并验证可复现 |
| 16:30-17:30 | 🔧 编写 `src/eval/cases/README.md`：Case 覆盖矩阵表、每个 Case 的一句话描述、标注说明、使用方式 | Case 目录文档完成 |

**Day 31 验收：** 8 个 Case 构建完成，每个 Case 的 bug 真实可复现，期望补丁可正确修复。

---

### Day 32（周二）：评测 Case 构建（下）+ 自动化 Runner

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 构建 Case 009-010（配置错误 + 综合性错误，困难）：Case 009 配置错误（`pyproject.toml` 依赖声明缺失导致构建失败）；Case 010 综合性错误（跨 2 个文件的类型错误 + 导入错误组合）。验证可复现 | 10 个 Case 全部完成 |
| 10:30-12:00 | ⚡ 实现 `src/eval/runner.py`：`EvalRunner` 类。`__init__(orchestrator_factory, cases_dir, output_dir)`。核心方法 `run_all()` → 遍历 `cases_dir` 下所有 Case 目录 → 对每个 Case：① 读取 `issue.txt` ② 复制 `repo/` 到临时目录（避免污染原始 Case）③ 调用 `orchestrator.repair(issue, tmp_repo)` ④ 在修复后的 repo 上运行 `pytest` 验证 ⑤ 记录 `CaseResult(case_id, fixed, retry_count, actual_patch, actual_lines, duration_ms, error, regression)` ⑥ 清理临时目录。生成 `eval_report.json` | `python -m src.eval.runner --all` 跑完 10 Case |
| 12:00-12:30 | 🔧 Runner 单测：用 Fake Orchestrator 预设 2 个 Case（1 成功 1 失败），验证结果记录正确 | `tests/test_eval_runner.py` 1 test green |
| 14:00-15:30 | ⚡ `CaseResult` 数据结构 + `eval_report.json` 格式定义：每个 Case 记录 `case_id, issue_type, difficulty, fixed(bool), retry_count, actual_patch(diff), actual_lines, minimal_lines, duration_ms, agent_timings{orchestrator, localizer, retriever, patcher, verifier}, error, introduced_regression(bool)`。report 顶层含 `summary{total, fixed, fix_rate, avg_retries, avg_duration}` + `by_type{}` + `by_difficulty{}` | report 格式完整 |
| 15:30-16:30 | ⚡ CLI 评测命令：`python -m src.cli eval --all` → 调 `EvalRunner.run_all()`；`--case case_001` → 单 Case 调试模式；`--verbose` → 打印每个 Case 的中间结果和各 Agent 耗时；`--output report.json` → 指定输出路径 | CLI eval 命令可用 |
| 16:30-17:30 | 🔧 用 1 个真实 Case 跑通 Runner（Fake Orchestrator），验证报告格式 | 端到端验证通过 |

**Day 32 验收：** 10 Case 全部构建完成。Runner 可自动化运行并生成结构化报告。CLI eval 命令可用。

---

### Day 33（周三）：Single-Agent Baseline + 消融实验框架

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `src/eval/baseline.py`：`create_single_agent_baseline(client, workspace)` 工厂函数。创建一个 Agent 实例，持有**全部 Tool**（ast_parse + stack_parse + search + read_file + write_file + patch_file + sandbox_build + sandbox_test + git_blame + git_diff + find_test），max_steps=12，approval="auto"。System Prompt：`"你是代码修复专家。分析错误、定位代码、生成补丁、在容器内验证修复。你可以使用所有工具。"` | Single-Agent 就位 |
| 10:30-12:00 | ⚡ 实现 `SingleAgentOrchestrator`：简化版编排器——收到 Issue → 直接调 Single-Agent 的 `ask(issue)` → Agent ReAct 循环自主完成定位→修补→验证。与 Multi-Agent Orchestrator 同接口 `.repair(issue, repo) -> RepairState` | 两种 Orchestrator 可互换 |
| 12:00-12:30 | 🔧 Single-Agent 单测（FakeClient 预设 ReAct 序列） | `tests/test_baseline.py` 1 test green |
| 14:00-15:30 | ⚡ 实现 `src/eval/ablation.py`：`AblationRunner` 类。`__init__(variants: dict[str, Orchestrator])` — key 为变体名。`run(cases_dir, repetitions=3)` → 对每种变体 × 每个 Case × 3 次重复 → 调用 `orchestrator.repair()` → 记录结果。输出 `ablation_report.json`：每种变体的 fix_rate、avg_retries、avg_duration 对比 | 3 变体 × 10 Case × 3 次 = 90 次实验 |
| 15:30-16:30 | ⚡ 定义 3 组消融变体：① `full` — 4 Agent（Localizer+Retriever+Patcher+Verifier）② `single` — Single-Agent（全量 Tool ReAct）③ `no_retriever` — 3 Agent（Localizer→Patcher→Verifier，跳过 Retriever） | 3 种 Orchestrator 均实现 `.repair()` 接口 |
| 16:30-17:30 | 🔧 Ablation 框架单测（Fake Orchestrator 预设固定结果） | `tests/test_ablation.py` 1 test green |

**Day 33 验收：** Single-Agent Baseline 就位。消融框架支持 3 变体 × 3 重复。所有变体共享 `.repair()` 接口。

---

### Day 34（周四）：评测首轮运行 + 指标计算

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-12:00 | ⚡ **首轮评测运行**：用真实 API 跑 3 个简单 Case（001-003）× 2 变体（full + single）× 1 次重复 = 6 次实验。目的：① 验证评测流水线畅通 ② 获取首轮指标数据 ③ 发现 Prompt 问题。**注意控制 API 费用**——6 次实验预估 2-3 元（DeepSeek） | 首轮 6 次实验完成 |
| 12:00-12:30 | 🔧 根据首轮结果调整：① 如果 Full 的 Fix Rate 异常低 → 检查各 Agent Prompt ② 如果 Single 异常高 → 检查 Case 是否太简单 ③ 记录 API 费用和时间 | 首轮问题记录 |
| 14:00-15:30 | ⚡ 实现 `src/eval/metrics.py`：`compute_metrics(results: list[CaseResult]) -> EvalReport`。指标：① `fix_rate = fixed / total` ② `first_attempt_rate = (retry_count==0) / total` ③ `avg_retries` ④ `patch_precision = sum(min_lines / max(actual_lines, 1)) / total` ⑤ `avg_duration_s` ⑥ `regression_rate`（引入新失败的比例）。`format_markdown(report)` → 生成 Markdown 表格 | 指标计算正确 |
| 15:30-16:30 | ⚡ 指标报告生成：① 总体指标表 ② 分变体对比表（full vs single vs no_retriever）③ 分 Case 明细表 ④ 分错误类型聚合（TypeError/ImportError/...）⑤ 分难度聚合（简单/中等/困难）。表格式直接可粘贴到 README | Markdown 报告可读 |
| 16:30-17:30 | 🔧 metrics 单测：用假数据验证所有指标计算 | `tests/test_metrics.py` 2 tests green |

**Day 34 验收：** 评测流水线真实 API 跑通。指标计算正确。Markdown 报告可读。

---

### Day 35（周五）：完整评测运行 + Prompt 调优 + 回归门禁

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-12:00 | ⚡ **正式评测运行**：2 变体（full + single，no_retriever 可选）× 10 Case × 3 次重复 = 60 次实验。**预估 DeepSeek API 费用 15-25 元，耗时 2-3 小时**。后台运行，期间可以做其他任务 | 60 次实验数据 |
| 12:00-12:30 | 🔧 检查评测结果：确认 Fix Rate 差异 > 15pp。如果差异不足 → 分析原因（Case 太简单/Case 太难/模型选择问题）。记录分析 | 数据分析 |
| 14:00-15:30 | ⚡ Prompt 调优冲刺：针对评测中 JSON 解析失败的 Case，分析 Agent 输出 → 调整对应 Agent 的 System Prompt（强化输出格式约束、加反面示例）。对调优后的 Prompt 重跑失败的 Case | JSON 解析成功率 ≥ 90% |
| 15:30-16:30 | ⚡ 实现 `src/eval/regression_check.py`：`RegressionChecker` 类。`check(current_report, baseline_report)` → 对比 fix_rate 变化，下降 > 5pp → 返回 `RegressionDetected(details)`；regression_rate 上升 > 3pp → 同上。`format_check_result()` → Markdown | 回归检测可用 |
| 16:30-17:30 | ⚡ M7 复盘 + git tag m7-done。**将最终评测数据写入 `eval_results/final_report.md`**——后续 M8 README 直接引用 | M7 正式完成，核心数据到手 |

**Day 35 验收（M7 里程碑）：**
- [ ] 10 Case 全部构建完成，每个可复现
- [ ] 自动化 Runner 可一次跑完所有 Case
- [ ] Single-Agent Baseline 实现
- [ ] 消融实验框架支持 3 变体 × 3 重复
- [ ] 完整评测运行：Multi-Agent Fix Rate ≥ 50%，vs Single-Agent ≥ +15pp
- [ ] JSON 解析成功率 ≥ 90%
- [ ] 回归门禁可用
- [ ] `pytest tests/ -v` 全绿（100+ tests）
- [ ] 代码量约 3400 行（Layer 1 1900 + M5 600 + M6 500 + M7 400）

---

### M7 周末缓冲

- 周六上午：如果正式评测数据不理想（差异 < 15pp），分析并调整 Case 或 Prompt
- 周六下午：补跑 no_retriever 变体（如果时间允许）
- 周日：休息

---

## M8：打磨、文档、Demo 与简历（Week 15-16）

**目标：让项目从"工程师能跑"变成"面试官 10 分钟看懂"。投递就绪。**

---

### Day 36（周一）：README 重写

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ README 结构设计 + 第 1-3 段：① 项目名 + 一句话描述 + 徽章（Python 3.11+ / pytest / ruff / Docker）② ASCII 架构图（Layer 1 Agent 运行时 + Layer 2 多 Agent 修复系统）③ "为什么与众不同"——与 LangChain 模板项目的 3 点对比 | 生人看前 3 段就知道这个项目是什么、为什么特别 |
| 10:30-12:00 | ⚡ README 第 4-5 段：快速开始——`git clone` → `pip install -e .` → `cp .env.example .env` → 填入 API key → `python -m agent_runtime "hello"`（Layer 1）→ `docker build` → `python -m src.cli repair --issue "..." --repo ./demo/calculator`（Layer 2）。每个命令旁边标注预期输出 | 生人 10 分钟可跑通 |
| 12:00-12:30 | 🔧 自己按 README 从零跑一遍，确认无遗漏步骤 | 自检通过 |
| 14:00-15:30 | ⚡ README 第 6-8 段：使用示例（Layer 1 one-shot / REPL / dry-run / resume；Layer 2 repair / verbose / ablation）、指标摘要表（Fix Rate 对比）、项目结构树（简化版）、依赖说明 | README 完整 |
| 15:30-16:30 | ⚡ README 润色：① 每段不超过 5 行 ② 代码块标注语言 ③ 关键数字加粗 ④ 添加锚点目录 | 排版清晰 |
| 16:30-17:30 | 🔧 把 README 发给一个朋友/同事看，收集"哪没看懂"的反馈 | 外部反馈 |

**Day 36 验收：** README 完整，生人 10 分钟可上手。有架构图、示例、指标数据。

---

### Day 37（周二）：ARCHITECTURE.md + ADR 设计决策记录

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ `ARCHITECTURE.md` 第 1-3 章：① Layer 1 Agent 运行时——17 个模块的职责说明（每个模块 3 句：职责、输入、输出、为什么存在）② 完整调用时序图（ASCII art：`user input → CLI → Agent.ask() → AgentLoop → ContextManager → Client → parse → ToolExecutor → 循环 → final`）③ 数据流图 | 架构文档前半完成 |
| 10:30-12:00 | ⚡ `ARCHITECTURE.md` 第 4-6 章：④ Layer 2 多 Agent 协作——Agent 协作图（Orchestrator → Localizer∥Retriever → Blackboard → Patcher → Verifier → 反馈）⑤ RepairState 在 Agent 间传递和变换 ⑥ 安全模型——5 层防护（路径锚定 → 审批 → 配额 → 容器隔离 → ToolGateway） | 架构文档完整 |
| 12:00-12:30 | 🔧 架构图检查：确认 ASCII 图在终端和 GitHub 上显示正常 | 排版检查 |
| 14:00-15:30 | ⚡ `docs/design-decisions.md`（ADR 格式）：记录 10 条关键设计决策。每条含 `Title` / `Status` / `Context`（背景 + 被拒绝的替代方案）/ `Decision`（选择什么）/ `Consequences`（带来的好处和代价）。10 条：① 为什么不用 LangChain ② 为什么 Agent 是独立实例而非子类 ③ 为什么 Token 预算用 tiktoken ④ 为什么用 Blackboard 而非消息传递 ⑤ 为什么 Skill 用字典而非 YAML（M5 阶段）⑥ 为什么容器网络默认关闭 ⑦ 为什么 10 个 Case 而非 36 个 ⑧ 为什么 Semantic Memory 用本地模型 ⑨ 为什么 Trace 用 JSONL 追加 ⑩ 为什么 PatchApplier 文件级回滚 | 10 条 ADR，每条 100-200 字 |
| 15:30-16:30 | ⚡ ADR 补充：每条 ADR 的 Consequences 部分写清楚"如果将来要改变这个决策，应该怎么做"。这展示向前兼容思维 | ADR 有演进路径 |
| 16:30-17:30 | 🔧 ADR 自审：每条 ADR 是否回答了"面试官会问什么" | ADR 可当面试应答稿 |

**Day 37 验收：** `ARCHITECTURE.md` 完整覆盖 Layer 1 和 Layer 2。10 条 ADR 可当面试应答。

---

### Day 38（周三）：Demo 脚本与录制

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 写 `demo/demo_1_repair.sh`：完整修复过程脚本。步骤：① 展示 `demo/calculator/` 项目结构 ② `cat issue.txt` 展示错误 ③ `pytest` 展示测试失败 ④ `python -m src.cli repair --issue "$(cat issue.txt)" --repo ./demo/calculator --verbose` ⑤ 展示生成的补丁 diff ⑥ `pytest` 展示测试全绿。脚本全程无需交互（使用 `--approval auto`） | 脚本可独立运行 |
| 10:30-11:30 | ⚡ 写 `demo/demo_2_self_healing.sh`：自愈循环脚本。使用 Case 006（需要 2 次尝试的逻辑错误）。步骤：① 展示 bug ② `repair --verbose` → 展示第 1 次补丁 → 验证失败 → 展示 feedback → 第 2 次补丁 → 验证通过 ③ 统计 `retry_count=2` | 脚本清晰展示自愈过程 |
| 11:30-12:00 | 🔧 两个脚本各自运行一遍，确认输出干净、无报错 | 脚本验证 |
| 14:00-15:00 | ⚡ 写 `demo/demo_3_ablation.sh`：消融实验对比脚本。步骤：① 快速说明 3 种变体 ② `python -m src.cli eval --ablation --variants multi,single --repetitions 1 --cases case_001,case_002,case_003`（快速版，约 5 分钟）③ 展示对比表 | 消融对比可视化 |
| 15:00-16:00 | ⚡ Demo 录制（OBS 或终端录屏工具）：录 3 段视频。每段 1-2 分钟。加简单字幕或旁白标注关键步骤。视频上传到项目 `demo/` 目录或外链 | 3 个 Demo 视频就绪 |
| 16:00-17:00 | 🔧 GitHub release 准备：将 3 个 Demo 视频上传到 GitHub Release（避免仓库太大），README 中引用外链 | Demo 可在线观看 |
| 17:00-17:30 | ⚡ 如果录视频不顺利 → 降级为 GIF 动图 + 脚本。GIF 比视频更轻量，GitHub README 可直接嵌入 | 演示材料就绪 |

**Day 38 验收：** 3 个 Demo 脚本可独立运行。演示材料（视频或 GIF）就绪。

---

### Day 39（周四）：CI/CD + 代码终审

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 创建 `.github/workflows/test.yml`：`push` → `ubuntu-latest` → `setup-python@v5` (3.11) → `pip install -e ".[dev]"` → `pytest -v --cov --cov-report=term` → `ruff check`。触发条件：push 到 main 和 PR | CI 绿灯 |
| 10:30-11:30 | ⚡ 创建 `.github/workflows/eval.yml`：`push` → `ubuntu-latest` → `docker build` → `python -m src.eval.runner --all --ci` → `python -m src.eval.regression_check` → 结果输出到 PR comment。触发条件：push 到 main（非 PR，因为耗时长） | 评测 CI 就绪 |
| 11:30-12:00 | 🔧 在自己的 repo 上 push 一次，确认两个 workflow 都触发并成功 | CI 验证 |
| 14:00-15:30 | ⚡ 代码终审：① `ruff check` + `ruff format` 零 warning ② 检查所有 `# TODO` 是否处理或标注为已知限制 ③ 检查所有公开函数有 docstring ④ 检查异常处理路径——有没有裸 `except:` ⑤ 检查硬编码路径——有没有写死 `/home/user/...` | 代码质量达标 |
| 15:30-16:30 | ⚡ 代码覆盖率终审：`pytest --cov=agent_runtime --cov=src --cov-report=html` → 检查覆盖率报告。核心模块（agent_loop, runtime, tool_executor, context_manager, orchestrator）覆盖率 > 80%。低覆盖率模块标注原因（如 `cli.py` 的交互部分难以单测） | 覆盖率报告可解释 |
| 16:30-17:30 | ⚡ `.gitignore` 终审 + 仓库清理：删除临时文件、清理 `__pycache__/`、确认 `.env` 不被追踪。`git ls-files` 确认无敏感文件 | 仓库干净 |

**Day 39 验收：** CI 绿灯。代码质量零 warning。覆盖率达标。仓库干净可公开。

---

### Day 40（周五）：简历定稿 + 最终验收 + 发布

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 简历 Bullet 终稿：从 20 点中精选 5-6 点，按"总→分→分→分→收"排列，每点控制在 2 行内。根据投递方向微调（AI 方向突出多 Agent 分工；基础架构方向突出 Docker Harness + 零依赖；安全方向突出 7 道闸口 + 容器隔离）。中英文各一版 | 简历 Bullet 终稿 |
| 10:30-11:30 | ~~⚡ GitHub 仓库设置（About / Topics / Pin）~~ | ~~GitHub 展示页就绪~~ **（M8D5 范围外）** |
| 11:30-12:00 | 🔧 用 incognito 浏览器打开自己的 GitHub，确认 README 渲染正确、架构图对齐 | 公开页面验证 |
| 14:00-15:00 | ⚡ M1-M8 最终验收清单：逐项检查 Day 5/10/15/20/25/30/35 的验收标准和里程碑。跑最后一次 `pytest tests/ -v` 确认全绿。统计最终数据：总行数、测试数、覆盖率、10 Case Fix Rate | 全部验收项通过 |
| 15:00-16:00 | ~~⚡ 项目总结文档 `PROJECT_SUMMARY.md`~~ | ~~总结文档就绪~~ **（M8D5 范围外；见 `docs/FINAL_STATS.md`）** |
| 16:00-17:00 | ⚡ 最终 git tag `v1.0.0` + push。写 Release Notes（基于 CHANGELOG 格式）：新功能、已知限制、运行说明。项目正式发布 | 项目发布 |
| 17:00-17:30 | 🎉 项目完成 | |

**Day 40 验收（M8 里程碑 / 项目完成）：**
- [ ] README 完整，生人 10 分钟可上手
- [ ] ARCHITECTURE.md + 10 条 ADR 完整
- [ ] 3 个 Demo 脚本可独立运行
- [ ] CI/CD 绿灯（test + eval 两个 workflow）
- [ ] 代码质量零 warning，覆盖率 > 70%
- [ ] 简历 Bullet 中英文终稿
- [ ] `pytest tests/ -v` 全绿（100+ tests）
- [ ] 最终代码量约 3800 行（Layer 1 1900 + M5 600 + M6 500 + M7 400 + M8 400）

> **M8D5 范围外（不纳入验收）：** Demo 视频/GIF；GitHub About / Topics / Release 页面配置；`PROJECT_SUMMARY.md`（由 `docs/FINAL_STATS.md` + `docs/RESUME_BULLETS.md` 替代）。

---

## 附录 A：M7-M8 新增文件清单

```
src/eval/
├── cases/
│   ├── case_001_type_error_simple/
│   ├── case_002_type_error_medium/
│   ├── case_003_type_error_hard/
│   ├── case_004_import_error_simple/
│   ├── case_005_import_error_medium/
│   ├── case_006_logic_error_medium/
│   ├── case_007_attribute_error_medium/
│   ├── case_008_logic_error_hard/
│   ├── case_009_config_error_hard/
│   ├── case_010_mixed_error_hard/
│   └── README.md
├── runner.py                # M7 Day32
├── baseline.py              # M7 Day33
├── ablation.py              # M7 Day33
├── metrics.py               # M7 Day34
└── regression_check.py      # M7 Day35

docs/
├── design-decisions.md      # M8 Day37（10 条 ADR）

demo/
├── demo_1_repair.sh         # M8 Day38
├── demo_2_self_healing.sh   # M8 Day38
└── demo_3_ablation.sh       # M8 Day38

.github/workflows/
├── test.yml                 # M8 Day39
└── eval.yml                 # M8 Day39

eval_results/
└── final_report.md          # M7 Day35

docs/FINAL_STATS.md            # M8 Day40（最终数据统计，替代 PROJECT_SUMMARY）
docs/RESUME_BULLETS.md         # M8 Day40
ARCHITECTURE.md              # M8 Day37

tests/
├── test_eval_runner.py      # M7 Day32
├── test_baseline.py         # M7 Day33
├── test_ablation.py         # M7 Day33
└── test_metrics.py          # M7 Day34
```

## 附录 B：M1-M8 全里程碑总览

| 里程碑 | 周 | 代码量 | 测试数 | 核心产出 |
|:--:|:--:|:--:|:--:|------|
| M1 | 1-2 | 500 | 20 | 控制循环 + 3 只读工具 + Config + Workspace |
| M2 | 3-4 | 1000 | 40 | 6 工具 + 7 闸口 + Token 预算 + Dry-Run + REPL |
| M3 | 5-6 | 1500 | 55 | 三层记忆 + Checkpoint + 持久化 + 安全 + 对话摘要 |
| M4 | 7-8 | 1900 | 70 | 语义记忆 + 配额 + 熔断 + Replay + 4 Provider |
| M5 | 9-10 | 2500 | 85 | 4 Agent + Blackboard + ToolGateway + AST/Stack/Git + Skill |
| M6 | 11-12 | 3000 | 95 | Docker Harness + Verifier + 自愈闭环 + 3 Demo |
| M7 | 13-14 | 3400 | 100 | 10 Case 评测集 + 消融实验 (90 次) + CI 回归门禁 |
| M8 | 15-16 | 3800 | 100 | README + 架构文档 + ADR + Demo 视频 + CI/CD + 简历 |

## 附录 C：项目完成后的面试准备清单

- [ ] 30 秒电梯演讲烂熟
- [ ] 2 分钟项目介绍可脱稿
- [ ] 10 条 ADR 中任一条可展开 2 分钟
- [ ] 能画出 Layer 1 控制循环时序图
- [ ] 能画出 Layer 2 Agent 协作图
- [ ] 消融实验数据可脱口而出
- [ ] Demo 视频链接在简历上可点击
- [ ] GitHub 仓库 README 在手机上显示正常
