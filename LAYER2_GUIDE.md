# Layer 2 导读 — 多 Agent 修复系统全貌

> 读完本文你将理解：186 个 Layer 2 专项测试 / 45 个源码文件 / ~5200 行代码的多 Agent 修复流水线是怎么组织的，每个模块干什么、怎么连接。  
> 前置阅读：[LAYER1_GUIDE.md](LAYER1_GUIDE.md)（Agent 运行时内核）。

---

## 1. 一分钟概览

```
用户提交 Issue（堆栈 + 描述）
    │
    ▼
CLI (src/cli.py) — repair / eval / ablation 子命令
    │
    ▼
repair_factory.py — ModelClient + Workspace → Patcher + 可选 Verifier
    │
    ├── agents/factory.py — 按角色装配 Agent + ToolGateway + 外置 prompt
    ├── middleware.py — ToolGateway 角色、阶段与证据权限
    └── tools/composite.py — L1 基础工具 + L2 域工具按 role 合并
    │
    ▼
Orchestrator (orchestrator.py + repair/pipeline.py) — 纯 Python 调度，不调 LLM
    │
    ├── _parse_issue() + _match_skill() → RepairPlan + YAML Skill
    ├── Plan DAG — 最多两路只读探索，汇集版本化证据
    ├── Patcher — 串行工具改盘，磁盘 diff 形成审计补丁
    └── Verifier — Docker / pytest 验证 → feedback 重试环（≤ max_retries）
    │
    ├── state.py — RepairState + 6 个结构化 dataclass 在 Agent 间流转
    ├── blackboard.py — KV 交换板 + 冲突检测（已实现，Orchestrator 目标深度接入）
    ├── harness/ — Docker 沙箱生命周期
    └── eval/ — 10 Case 消融评测（full / single / no_retriever）
```

**与 Layer 1 的关系**：每个修复 Agent 都是 `agent_runtime.Agent` 的独立实例。Orchestrator 负责运行协调、任务派发、状态机与验证，模型负责理解问题和生成修改。

---

## 2. 文件地图（按功能分组）

> 行数为 `src/` 生产代码（**不含** `eval/cases/` 内 demo repo）。Skills / Prompts 为文本资源，未计入 `.py` 统计。

### 2.1 入口与装配

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `cli.py` | 226 | M7/M8 | `repair` / `eval` / `ablation` 子命令 + 报告输出 |
| `repair_factory.py` | 88 | M8 | `wire_orchestrator()` · `make_orchestrator_factory()` · Docker Verifier 探测 |
| `agents/factory.py` | 107 | M5/M7 | `create_repair_agent()` — 四角色 + baseline 单 Agent 变体 |
| `agents/localizer.py` 等 | 5×4 | M5 | 薄包装，导出 `create_*` |
| `prompts/loader.py` | 11 | M5 | 加载 `src/prompts/{role}.txt` |
| `prompts/*.txt` | — | M5 | Localizer / Retriever / Patcher / Verifier 外置 system prompt |
| `skills/*.yaml` | — | M5 | 4 个 Skill：`trigger_pattern` + 策略 + 建议工具链 |

### 2.2 编排与状态

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `orchestrator.py` | 676 | M5/M6/M8 | 主调度器：Issue 解析 · Skill 匹配 · prompt 模板 · patch/verify 循环 |
| `repair/pipeline.py` | 203 | M8 | `RepairPipelineMixin` — localize∥retrieve 并行 + `_repair_impl` 主循环 |
| `state.py` | 314 | M5 | `RepairState` + `SuspectLocation` / `RepairPlan` / `RetrievedContext` / `CandidatePatch` / `VerificationResult` |
| `blackboard.py` | 108 | M5 | KV 黑板 · TTL · 冲突记录 · `read_related(prefix)` |
| `middleware.py` | 67 | M5 | `ToolGateway` + `REPAIR_PERMISSION_TABLE` · `build_repair_gateway()` |

### 2.3 L2 领域工具

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `tools/composite.py` | 55 | M5/M6 | 按 role 合并 L1 `build_tool_registry` + L2 工具 + sandbox 工具 |
| `tools/registry.py` | 69 | M5 | `build_repair_tools()` — ast/stack/git/find_test 注册 |
| `tools/ast_parser.py` | 104 | M5 | Python AST 结构化解析（函数/类/方法） |
| `tools/stack_parser.py` | 71 | M5 | Traceback → 结构化帧列表 |
| `tools/git_tools.py` | 102 | M5 | `git_blame` · `git_diff` |
| `tools/find_test.py` | 87 | M5 | 按函数名定位测试文件/用例 |
| `tools/sandbox_tools.py` | 185 | M6 | `sandbox_build` / `sandbox_test` / `sandbox_verify` Agent 工具 |

### 2.4 Harness · Patch · Verify

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `harness/sandbox_manager.py` | 164 | M6 | Docker 容器 create/destroy · tar 传 `/code` · mem/cpu 限制 |
| `harness/python_runner.py` | 93 | M6 | 容器内 pytest 执行封装 |
| `harness/patch_applier.py` | 67 | M6 | 宿主机侧 patch 辅助 |
| `repair/patch_applier.py` | 318 | M6/M8 | `PatchApplier` — diff 解析 · 多 hunk 应用 · 行级校验 |
| `repair/output_parsers.py` | 51 | M6/M8 | Localizer/Retriever/Patcher/Verifier JSON 解析 + 降级 |
| `repair/repo_snapshot.py` | 69 | M6 | verify 前文件快照 · 失败回滚 |
| `repair/verify.py` | 93 | M6/M8 | `VerifyStrategy` — `DockerVerifyStrategy` / `PytestVerifyStrategy` |

### 2.5 评测与消融

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `eval/runner.py` | 246 | M7 | `EvalRunner` — Case 遍历 · temp repo 隔离 · pytest 验收 |
| `eval/ablation.py` | 237 | M7 | 多变体 × 多 Case × repetitions 批量跑 |
| `eval/variants.py` | 74 | M7 | `full` / `single` / `no_retriever` 工厂 |
| `eval/baseline.py` | 104 | M7 | 单 Agent baseline Orchestrator 包装 |
| `eval/fake_runner.py` | 46 | M7 | Fake Orchestrator（应用 expected_patch，零 API） |
| `eval/metrics.py` | 353 | M7 | Fix Rate · Patch 精度 · Token · 耗时聚合 |
| `eval/models.py` | 73 | M7 | `CaseResult` · `EvalReport` dataclass |
| `eval/token_usage.py` | 141 | M7/M8 | 跨 Agent token 汇总进评测报告 |
| `eval/regression_check.py` | 259 | M7 | 正式结果 vs 基线回归门禁 |
| `eval/patch_utils.py` | 62 | M7 | unified diff 应用辅助 |
| `eval/cli_helpers.py` | 170 | M7 | eval/ablation CLI 共用打印与调度 |
| `eval/__main__.py` | 67 | M7 | `python -m src.eval` 入口 |
| `eval/cases/case_*/` | — | M7 | 10 个迷你 repo + `metadata.yaml` + `expected_patch` |

---

## 3. 数据流追踪

### 3.1 一次 repair() 的完整路径

```text
CLI → make_orchestrator_factory → wire_orchestrator
        ├─ 解析统一运行时配置、WorkspaceContext 和模型客户端
        ├─ create_patcher，绑定规范工具与 ToolGateway
        └─ 按验证策略装配独立 Verifier

Orchestrator.repair
  → RepairState / 取消令牌 / 运行协调 / checkpoint 恢复
  → Issue 解析、规则种子与上下文组装
  → RepairPlanBinding 管理证据驱动 Plan DAG
      ├─ 最多两路只读探索，返回版本化证据
      └─ 主 Patcher 串行 read_file → apply_patch → quick_test
  → 磁盘快照 diff → CandidatePatch（审计与导出）
  → Critic → 独立 Verifier
      ├─ 跳过验证：pending_verify
      ├─ 通过：fixed
      └─ 失败：回滚、反馈与有限重试
  → 保存当前版本状态、工具收据、Plan seal 与报告
```

Patcher 的自然语言结论不会被再次解析成补丁落盘。每次 Agent 调用经过 Layer 1
`AgentLoop`、上下文治理、预算和工具安全闸口。旧 checkpoint 不自动迁移，恢复边界
见 [运行时契约](docs/RUNTIME_CONTRACTS.md) 与 [Plan DAG](docs/PLAN_DAG.md)。

### 3.2 当前角色与权限

| 角色 | 职责 | 权限 |
|------|------|------|
| Patcher | 探索、修改、快检 | 受读前写、编辑锁、预算和审批约束 |
| 只读探索 Agent | 执行派发的探索节点 | 只读工具，不修改仓库 |
| Critic | 提交前轻量检查 | 运行时裁决 |
| Verifier | 独立验证 | 验证策略与沙箱工具，不生成代码修改 |

`ToolGateway` 裁决角色、阶段、证据及工具权限；`Agent.execute_tool()` 进入
`ToolExecutor.execute_gated()` 执行闸口。Localizer/Retriever 不再作为两个独立 LLM
Agent 装配进当前主流程。

### 3.3 各模块编写顺序

```
M5 (多 Agent 骨架):
  state(6 dataclass) → blackboard → middleware(ToolGateway)
  → tools/registry(+5) → agents/factory → prompts/*.txt
  → orchestrator(parse+localize+retrieve+patcher) → skills/*.yaml

M6 (沙箱 + 自愈闭环):
  harness/sandbox_manager → sandbox_tools → verifier agent
  → repair/patch_applier → repo_snapshot → output_parsers
  → orchestrator(verify loop + feedback + retry)

M7 (评测 + 消融):
  eval/cases(10) → runner → metrics → baseline/single
  → variants(full/single/no_retriever) → ablation → regression_check
  → cli(eval/ablation) → token_usage

M8 (重构 + 交付):
  repair/pipeline(RepairPipelineMixin) → repair_factory
  → verify(Docker/Pytest Strategy) → orchestrator 瘦身
  → demo 脚本 · ADR · CODE_REVIEW · FINAL_STATS
```

---

## 4. 关键设计模式

### 4.1 Orchestrator 不调 LLM

Issue 解析（正则）、Skill 匹配（YAML）、并行调度、重试环、快照回滚全是纯 Python。LLM 只出现在四个 Agent 的 `ask()` 内——面试时可强调「编排与推理分离」。

### 4.2 结构化 State，不靠自然语言协议

Agent 产出经 `output_parsers` 解析为 dataclass，写入 `RepairState` 字段后再拼下一轮 prompt。`SuspectLocation` / `CandidatePatch` 等带 `to_dict()` / `from_dict()` + `schema_version`，可序列化进 trace / eval 报告。

### 4.3 ToolGateway 对 Agent 透明

越权调用返回普通 `ToolResult(error_code="permission_denied")`，不抛异常。Agent 读错误信息自行调整，与 Layer 1「闸口不崩溃循环」一致。

### 4.4 Template Method + Strategy

- `RepairPipelineMixin` 承担修复控制流，`RepairPlanBinding` 负责 Plan 派发和恢复，验证采用独立策略。
- `VerifyStrategy` 抽象 Docker vs 宿主机 pytest，eval 默认 pytest、生产 repair 优先 Docker。

### 4.5 读并行、写串行、验证前快照

Plan 节点支持最多两路只读探索；主 Patcher 串行写 workspace；验证失败后恢复快照，再进入反馈环。

### 4.6 工厂贯穿 repair 与 eval

`make_orchestrator_factory()` / `build_ablation_variants()` 统一装配路径，保证 CLI repair、eval runner、ablation 使用同一套 Agent  wiring。

---

## 5. 测试地图

| 文件 | 测试 | 覆盖模块 |
|------|:--:|------|
| `test_state.py` | 7 | RepairState + dataclass 序列化 |
| `test_blackboard.py` | 6 | 写入/冲突/TTL/前缀读 |
| `test_middleware.py` | 5 | ToolGateway 权限表 |
| `test_agents_m5.py` | 9 | 四角色 Agent 工具集 + gateway |
| `test_prompts_m5.py` | 10 | prompt 加载与注入 |
| `test_ast_parser.py` | 3 | AST 解析工具 |
| `test_repair_tools.py` | 9 | git/stack/find_test 等 |
| `test_orchestrator.py` | 17 | 解析 · 流水线 · mock Agent |
| `test_orchestrator_robustness.py` | 3 | 超时 · 边界 · 降级 |
| `test_no_retriever.py` | 2 | 3-Agent 变体 |
| `test_sandbox_manager.py` | 6 | Docker 生命周期（mock） |
| `test_sandbox_tools.py` | 6 | sandbox_* Agent 工具 |
| `test_verifier.py` | 4 | Verifier Agent + verify 策略 |
| `test_e2e_repair.py` | 6 | 端到端 repair（mock LLM） |
| `test_eval_cases.py` | 51 | Case 元数据 + repo 结构 |
| `test_eval_runner.py` | 15 | EvalRunner + pytest 验收 |
| `test_metrics.py` | 4 | 指标聚合 |
| `test_token_usage.py` | 5 | 跨 Agent token |
| `test_baseline.py` | 4 | 单 Agent baseline |
| `test_ablation.py` | 2 | 消融变体工厂 |
| `test_regression_check.py` | 4 | 回归门禁 |
| `test_cli_repair.py` | 2 | repair CLI |
| `test_cli_eval.py` | 4 | eval CLI |
| `test_cli_ablation.py` | 2 | ablation CLI |

**25 个 Layer 2 专项测试文件，186 个测试**（全项目合计 **476** 个测试，见 `docs/FINAL_STATS.md`）。

---

## 6. 运行时产物

### 6.1 单次 repair

Orchestrator 本身不写独立 run 目录；各 Agent 的 `ask()` 仍产出 Layer 1 标准工件：

```
.agent/runs/{run_id}/
├── task_state.json
├── trace.jsonl
└── report.json
```

`RepairState.node_timings` / `token_usage` 在 CLI 侧打印或进入 eval 报告；目标增强见 `docs/bonus.md` §12（repair 落盘 `repair_state.json`）。

### 6.2 评测 / 消融

```
eval_results/
├── report.json          # eval 单次汇总
├── report.md            # --markdown 生成
├── ablation.json        # 消融原始结果
└── final_report.md      # 正式 60 runs 报告（本地，gitignore）
```

Case 库结构：

```
src/eval/cases/case_001/
├── metadata.yaml        # issue_type · difficulty · requires_retriever
├── issue.txt            # 输入 Issue
├── expected_patch.diff  # 期望补丁（fake / 指标用）
└── repo/                # 迷你 Python 项目 + pytest
```

---

## 7. 快速启动

```bash
conda activate fixloop

# 修复单个 Issue（需 API + 可选 Docker）
python -m src.cli repair --issue "$(cat issue.txt)" --repo ./myproject --verbose

# 演习：工具不真正写入
python -m src.cli repair --issue "..." --repo . --dry-run

# 跳过 Docker/pytest 验证（仅生成补丁）
python -m src.cli repair --issue "..." --repo . --skip-verify

# 跑单个评测 Case
python -m src.cli eval --case case_001 --verbose

# 跑全部 Case + Markdown 报告
python -m src.cli eval --all --markdown --output eval_results

# 零 API 冒烟（Fake Orchestrator）
python -m src.cli eval --case case_001 --fake

# 消融实验（full vs single vs no_retriever，默认每 Case 3 次）
python -m src.cli ablation --all --repetitions 3 --output eval_results

# Layer 2 相关测试
pytest tests/test_orchestrator.py tests/test_eval_runner.py tests/test_sandbox_tools.py -v

# 全量测试 + 覆盖率（Layer 1 + 2）
pytest tests/ -v --cov=agent_runtime --cov=src --cov-report=term

# Lint
ruff check agent_runtime src tests
```

**Docker 沙箱**（Verifier 可选）：构建镜像 `repair-agent/python-repair`（见 `sandbox/` 目录与 README），`docker info` 可用时 `repair_factory` 自动挂载 Verifier。

---

## 8. 与 Layer 1 的边界对照

| 维度 | Layer 1 | Layer 2 |
|------|---------|---------|
| 入口 | `python -m agent_runtime` | `python -m src.cli` |
| Agent 数 | 1 通用 | 4（+ baseline 变体） |
| 编排 | AgentLoop while 循环 | Orchestrator 阶段机 + 重试环 |
| 工具 | 6 个通用 | +5 域工具 + 3 sandbox 工具 |
| 权限 | allowed_tools 列表 | ToolGateway 角色表 |
| 状态 | TaskState + session | RepairState + Blackboard |
| 验证 | 无 | Docker / pytest + 快照回滚 |
| 评测 | 无 | 10 Case · 消融 · 回归门禁 |

---

*Layer 2 完成 | 全项目 476 tests | 80% 行覆盖 | Layer 2 ~5200 行源码 | M5–M8 · PR #85 基线*
