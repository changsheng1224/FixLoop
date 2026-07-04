# FixLoop 设计模式 / 代码质量重构计划

> **Phase 0 产出**（只读审计，2026-07-04）  
> 前置：`docs/refactor/L1-L2-BOUNDARY.md` Phase 1–4 已合入 `master`（PR #82，`d6dbe5b`）  
> **约束**：行为不变、L2→L1 only、482 pytest 全绿、ruff 0 warning、每 Phase PR <400 行

参考：[ARCHITECTURE.md](../../ARCHITECTURE.md)、[design-decisions.md](../design-decisions.md)、[L1-L2-BOUNDARY.md](./L1-L2-BOUNDARY.md)

---

## 1. 与 L1/L2 边界重构的对齐

### 1.1 Phase 1–4 已解决项（勿重复）

| 原编号 | 坏味道 | 模式 / API | 状态 |
|--------|--------|------------|------|
| R1 | Duplicated Code：dotenv + ModelClient | **Facade** → `agent_runtime.bootstrap` | ✅ |
| R2 | Duplicated Code：工具 merge/pop | **Composite** → `build_repair_agent_tools(role)` | ✅ |
| R3 | Duplicated Code：四个 `create_*` | **Factory** → `src/agents/factory.py` | ✅ |
| R4 | Feature Envy：Patcher 直调 `model_client` | **L1 入口** → `Agent.complete_once()` | ✅ |

### 1.2 当前 L2→L1 接触面（post #82）

| L1 符号 | L2 消费文件数 | 说明 |
|---------|:------------:|------|
| `bootstrap` | 2 | `repair_factory`, `src/cli` ✓ 已收敛 |
| `runtime.Agent` | 2 | `factory`, `baseline` |
| `config` | 2 | 同上 |
| `tool_context` | 4 | `factory`, `composite`, `registry`, `sandbox_tools` |
| `tools.build_tool_registry` | 1 | 仅 `composite` ✓ |
| `workspace` | 2 | `repair_factory`, `baseline` |
| `schema_utils` | 1 | `tools/registry` |
| `tool_executor` (lazy) | 1 | `middleware` — **待 Phase A 消除 patch 需求** |

**L1 import L2：0**（方向仍正确）

### 1.3 剩余架构偏离

| 偏离 | 位置 | 严重度 |
|------|------|:------:|
| 运行时 monkey-patch `execute_tool` | `middleware.wrap_agent` | 高 |
| God class 编排 + 解析 + 补丁 + 验证 | `orchestrator.py`（1240 行，40+ 方法） | 高 |
| Baseline 借用空 Orchestrator 调 patch 逻辑 | `baseline.py:107` | 中 |
| Blackboard 未接入主路径（仅单测） | `blackboard.py` vs `orchestrator` | 中（设计债，非 bug） |
| Verify 双实现（Docker 直连 vs pytest vs LLM Verifier） | `orchestrator._run_*_verifier` | 中 |

---

## 2. Top 10 代码坏味道

| # | 坏味道 | 位置（证据） | 严重度 | 变化点 |
|---|--------|--------------|:------:|--------|
| **S1** | **God Class** | `src/orchestrator.py` 1240 行；含 issue 解析、prompt 组装、JSON 解析、patch 应用、快照回滚、verify、token 统计 | **高** | 编排步骤 vs 领域能力混在一起 |
| **S2** | **Monkey Patch / 权限在 L1 闸口外** | `middleware.py:47-55` 替换 `agent.execute_tool`；测试需 patch 绑定名 | **高** | 权限检查应在 `ToolExecutor` 内 |
| **S3** | **Feature Envy** | `baseline.py` 构造 `Orchestrator(None,None,None)` 调用 `_parse_patches` / `_apply_patches_on_disk` | **高** | patch 应用属独立领域，不应依赖编排器 |
| **S4** | **Duplicated Code（Verify 路径）** | `_run_docker_verifier` + `_run_pytest_verifier` 均写 timing / `VerificationResult` / stderr；与 `eval/runner.run_pytest` 部分重叠 | **中** | 验证后端可替换 |
| **S5** | **Duplicated Code（JSON 解析）** | `_parse_suspect_list` / `_parse_retrieved_context` / `_parse_patches` / `_parse_verification` 均 `_extract_json_block` + `json.loads` + try/except | **中** | Agent 输出格式多样但解析骨架相同 |
| **S6** | **Duplicated Code（Repo 快照）** | `orchestrator._snapshot_repo`（全 repo 除 skip dirs）vs `baseline._snapshot_source_tree`（eval diff 过滤） | **中** | 快照策略不同，API 应统一 |
| **S7** | **Long Method** | `orchestrator._repair_impl`（~110 行）、`_patcher_prompt`（~80 行） | **中** | 重试循环 + 多阶段副作用 |
| **S8** | **Parallel Inheritance / 变体扩展成本高** | `eval/variants.py::NoRetrieverOrchestrator` 覆写 `_run_localize_and_retrieve` 复制 localizer 分支 | **中** | 流水线步骤组合 |
| **S9** | **Speculative Generality（Dead in prod）** | `blackboard.py` 108 行；生产路径用 `RepairState` 直传（ADR-004）；仅 `tests/test_blackboard.py` | **低–中** | 保留模块 vs 标记 deprecated |
| **S10** | **Duplicated Code（Baseline 装配）** | `baseline.create_single_agent_baseline` 与 `factory.create_repair_agent` 仍各自 `Agent(...)` + 独立 gateway 表 | **低–中** | baseline 可视为 factory 的 `"baseline"` role |

**未进 Top 10 但记录：**

- `_complete_with_system_prompt` 仍保留无用参数 `prompt_name`（Phase 3 遗留薄包装）→ 可内联为 `_complete_once`
- `src/agents/{localizer,...}.py` 6 行 re-export → **可接受 Facade**（稳定 import 路径），勿再拆文件
- `tools/registry.py` 5 段相同 registry 字面量 → 可用小型 **Registry Builder**（优先级低）

---

## 3. 坏味道 → 设计模式映射

| Smell | 候选模式 | 为何适用（变化的是什么） | 归属 |
|-------|----------|--------------------------|------|
| **S1** God Class | **Template Method** + **Extract Class** | 变化：流水线步骤顺序固定，每步实现可拆 | L2：`orchestrator/` 包或 mixin |
| **S2** Monkey Patch | **Chain of Responsibility / Gateway** | 变化：权限规则表；执行链位置固定 | L1：`ToolExecutor` 前置 hook；L2：只提供 `REPAIR_PERMISSION_TABLE` |
| **S3** Feature Envy | **Extract Service** | 变化：patch 解析/应用算法 | L2：`src/repair/patch_applier.py`（纯函数/类） |
| **S4** Verify 重复 | **Strategy** | 变化：Docker / pytest / LLM 验证后端 | L2：`VerifyStrategy` 协议 + 3 实现 |
| **S5** JSON 解析重复 | **Template Method** 或 **Parser 注册表** | 变化：目标 dataclass 类型 | L2：`src/repair/output_parsers.py` |
| **S6** 快照重复 | **Strategy** 或 **参数化 Snapshot** | 变化：包含/排除规则 | L2：`src/repair/repo_snapshot.py` |
| **S7** Long Method | **Extract Method**（Template Method 子步骤） | 变化：无新抽象，仅拆函数 | L2：同 S1 |
| **S8** 变体 Orchestrator | **Template Method**（钩子方法） | 变化：是否并行 Retriever | L2：基类定义步骤，变体覆写单步 |
| **S9** Blackboard | **不强行接入** 或 **Facade 到 RepairState** | ADR-004 已决策主路径 | 文档标记；Phase F 可选 deprecated |
| **S10** Baseline 装配 | **Factory Method** 扩展 | 变化：`baseline` role 的 config + gateway | L2：扩展 `factory.create_repair_agent("baseline")` |

**禁止：** 为每个 `_parse_*` 各建一个类层次（过度 OOP）；无测试的 orchestrator 全文件搬家。

---

## 4. 迁移 Phase（建议顺序）

每 Phase：**一个模式 / 一个边界 + 删除一处明确重复**；独立 PR；squash merge。

| Phase | 分支建议 | Smell | 模式 / 产出 | 预估行数 | 测试 |
|-------|----------|-------|-------------|:--------:|------|
| **A** | `refactor/L1-tool-policy` | S2 | L1 `ToolExecutor` + `Agent(agent_name=, tool_policy=)`；删 `wrap_agent` | ~220 | `test_middleware`, `test_tool_executor`, `test_agents_m5` |
| **B** | `refactor/L2-verify-strategy` | S4 | `VerifyStrategy` + `DockerVerify` / `PytestVerify`；orchestrator 委托 | ~180 | `test_orchestrator`, `test_sandbox_tools`, `test_eval_runner` |
| **C** | `refactor/L2-patch-applier` | S3 | `PatchApplier` 服务；baseline 不再 `Orchestrator(None,...)` | ~150 | `test_baseline`, `test_orchestrator`, e2e repair |
| **D** | `refactor/L2-output-parsers` | S5 | 统一 JSON 解析；删 4 处重复 try/except | ~120 | `test_orchestrator` |
| **E** | `refactor/L2-orchestrator-split` | S1,S7,S8 | Template Method：`RepairPipeline` 基类 + 步骤模块 | ~350 | `test_orchestrator`, `test_no_retriever`, e2e |
| **F** | `refactor/L2-baseline-factory` | S10 | `factory` 支持 baseline role；删 baseline 重复 Agent 构造 | ~100 | `test_baseline` |
| **G** | `refactor/L2-repo-snapshot` | S6 | `repo_snapshot.py` 参数化 filter | ~80 | orchestrator robustness |
| **H** | `docs/dead-code-blackboard` | S9 | 文档标注 ADR-004 现状；**不删** blackboard（有测试+ADR） | ~40 | 无代码或仅 doc |

### 4.1 推荐必做集合（质量 / 可测性 ROI）

```
Phase A（权限进 L1）→ Phase B（Verify Strategy）→ Phase C（PatchApplier）
```

完成后：monkey-patch 消失、verify 可单测、baseline 不依赖 God class。

### 4.2 Phase A 详细预览（建议 Phase 1 执行）

**L1 API 草图：**

```python
# agent_runtime/tool_executor.py
class ToolExecutor:
    def __init__(..., tool_policy: Callable[[str, str], bool] | None = None):
        ...

    def execute(self, name, args):
        if self._tool_policy and not self._tool_policy(self._agent_name, name):
            return ToolExecutionResult(..., tool_error_code="permission_denied")
        ...

# agent_runtime/runtime.py — Agent.__init__
def __init__(..., agent_name: str = "", tool_policy=None):
    self._agent_name = agent_name
    ...
```

**L2 变更：**

- `factory.create_repair_agent` 传 `agent_name=role`, `tool_policy=gw.can_call` 闭包
- 删除 `middleware.wrap_agent` 调用（保留 `ToolGateway` 类作 policy 表）
- **删除重复**：`wrap_agent` monkey-patch 整段

**回滚：** revert PR；行为应完全一致（permission_denied 文案可保持）。

### 4.3 Phase B 详细预览

```python
# src/repair/verify.py
class VerifyStrategy(Protocol):
    def verify(self, repo_root: str, state: RepairState) -> VerificationResult: ...

class DockerVerifyStrategy: ...  # 包装 run_sandbox_verification
class PytestVerifyStrategy: ...    # 包装 run_pytest
```

Orchestrator `_run_verifier` → 选 strategy，删 `_run_docker_verifier` / `_run_pytest_verifier` 重复 timing 块。

### 4.4 Phase C 详细预览

```python
# src/repair/patch_applier.py
def parse_patches(text: str) -> list[CandidatePatch]: ...  # 从 orchestrator 迁出
def apply_patches(repo_root: str, patches: list[CandidatePatch]) -> list[CandidatePatch]: ...
```

`baseline` 与 `orchestrator` 共用；删除 `Orchestrator(None, None, None)`。

---

## 5. 风险项

| 风险 | 影响 | 缓解 |
|------|------|------|
| Phase A 改变 permission 检查时机 | 极少数 edge case（patch 前后 policy） | 保持「仅 name+tool 二维表」；对照 `test_agents_m5` 越权用例 |
| Phase E orchestrator 拆分 | 合并冲突、行为回归 | 最后做；每步 extract 先搬纯函数，再改类结构 |
| Phase F baseline 并入 factory | gateway 表语义变化 | baseline 仍用 `{tool: {baseline}}`，仅搬构造 |
| 删除 Blackboard | 破坏 ADR/简历叙事 | **Phase H 只文档化，不删代码** |
| PR 超 400 行 | 审查困难 | Phase E 拆 E1（parsers+patch）+ E2（pipeline class） |

---

## 6. 验收标准（全程）

- [ ] `pytest tests/ -v` → 482+ passed
- [ ] `ruff check agent_runtime src tests` → 0 warning
- [ ] `rg "from src" agent_runtime/` → 0
- [ ] CLI / eval 指标口径不变
- [ ] 每 PR diff < 400 行（文档 PR 除外）

---

## 7. Phase 0 结论

1. **L1/L2 边界 Phase 1–4 已解决主要「装配重复」**；下一轮主轴是 **可测试性 + God class 拆分**。
2. **最高 ROI**：S2（monkey-patch）→ S4（Verify Strategy）→ S3（PatchApplier 抽离）。
3. **设计模式选型原则**：Orchestrator 内「步骤顺序固定、步骤实现多变」→ Template Method；「验证后端可切换」→ Strategy；「权限检查入闸口」→ Chain of Responsibility。
4. **Blackboard** 按 ADR-004 保持辅路，不作为本轮必做 refactor 目标。

---

*文档版本：Phase 0 · base `master` @ `d6dbe5b`（PR #82）*
