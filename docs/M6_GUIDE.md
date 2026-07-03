# M6 GUIDE — Docker 沙箱 + Verifier + E2E 闭环

> M6 在 M5 多 Agent 流水线之上，补齐 **Docker 容器验证**、**真实 API 端到端修复** 与 **鲁棒性保护**。主线截至 `e49ef24`（PR [#68](https://github.com/changsheng1224/FixLoop/pull/68)）。

---

## 1. PR 时间线（M6D1–M6D4）

| PR | Commit | 主题 |
|----|--------|------|
| [#62](https://github.com/changsheng1224/FixLoop/pull/62) | M6D1 | Docker 沙箱：`SandboxManager` / `PatchApplier` / `PythonTestRunner` |
| [#63](https://github.com/changsheng1224/FixLoop/pull/63) | M6D2 | `sandbox_tools` + Verifier Agent + 自愈循环 |
| [#64–#65](https://github.com/changsheng1224/FixLoop/pull/65) | `477e43f` | M6D3：`demo/calculator`、E2E 加固、`node_timings`、项目规范 |
| [#66](https://github.com/changsheng1224/FixLoop/pull/66) | `b8d942d` | M6D4：L∥R 并行、HF 离线语义记忆、Patcher prompt 强化 |
| [#67](https://github.com/changsheng1224/FixLoop/pull/67) | `f350c47` | M6D4：`demo/importer` + `demo/logic_bug`、import 降级定位 |
| [#68](https://github.com/changsheng1224/FixLoop/pull/68) | `e49ef24` | M6D4：180s 超时、`agent_errors` 降级、沙箱 `try/finally` destroy |

**当前指标：** `pytest tests/ -v` — **359 passed**；`--cov=agent_runtime --cov=src` — **74%** 总覆盖率。

---

## 2. M6 相对 M5：新增能力

| 能力 | M5 状态 | M6 新增 |
|------|---------|---------|
| 容器执行 | 无 | **Docker 沙箱**（tar 传文件 + `entrypoint.sh` build/test） |
| 验证 | 预留 | **Verifier** 直连 `run_sandbox_verification()`（非 LLM loop） |
| 自愈闭环 | 设计 | **Patcher ↔ Verifier** 最多 3 轮，失败 `git checkout` 回滚 + 反馈 |
| Demo | 无 | **`demo/calculator`**、**`demo/importer`**、**`demo/logic_bug`** |
| 并行 | 串行 | **Localizer ∥ Retriever**（`ThreadPoolExecutor`，墙钟 ~16–18s） |
| 鲁棒性 | 无 | **180s 全流程超时**、`RepairState.agent_errors`、沙箱超时与 destroy 保护 |
| E2E 脚本 | 无 | **`demo/demo_repair.sh`**（跑 repair 后 `git checkout` 恢复 bug 状态） |

---

## 3. 核心改动（按模块）

### 3.1 Orchestrator

| 改动 | 说明 |
|------|------|
| Patcher 直连 API | 一次 `complete()` + JSON 解析，Orchestrator 宿主机落盘 |
| Verifier 直连 harness | `run_sandbox_verification()`，不经 Verifier LLM |
| L ∥ R 并行 | `_run_localize_and_retrieve()`，`localize_retrieve_ms` 为墙钟耗时 |
| 降级定位 | Localizer 无输出 → `_fallback_suspects_from_plan()`（含 import 行号） |
| 路径校验 | `_resolve_repo_file()` 拒绝 repo 外补丁 |
| 全流程超时 | `repair(timeout_s=180)`，超时 `status=failed` + `agent_errors["orchestrator"]` |
| Agent 失败降级 | Localizer / Retriever / Patcher / Verifier 各自 `try/except` → `agent_errors` |

### 3.2 Docker 沙箱

| 项 | 说明 |
|----|------|
| 生命周期 | `entrypoint=""` + `sleep infinity`；tar 传文件后 `/entrypoint.sh` 执行 |
| 超时 | `BUILD_TIMEOUT_S=600`，`TEST_TIMEOUT_S=900`；`execute()` 真正 enforce timeout |
| 清理 | `_run_test_in_sandbox()` `try/finally` 确保 `destroy()` |
| 假通过修复 | `all_passed` 要求 `total_tests > 0`；报告缺失 → `all_passed=False` |

### 3.3 Layer 1 增强（PR #66）

- **HF 离线**：`SemanticMemory` 检测本地 cache 后自动 `HF_HUB_OFFLINE=1`
- **懒加载**：`Agent.semantic_memory` 首次访问才加载模型
- **Agent loop**：`_log_loop()` 统一 stderr 阶段日志

### 3.4 CLI

- `--repo` 作为各 Agent `cwd`
- `--verbose` 打印 `node_timings` 分项
- Docker 不可用时自动跳过 Verifier（`status=patched`）

---

## 4. 端到端架构（当前）

```
Issue + --repo ./demo/<project>
        │
        ▼
┌──────────────────────────────────────────────────┐
│ Orchestrator  (repair_timeout_s=180)              │
│  parse_issue → Skill 匹配                         │
│       │                                           │
│       ├── Localizer ──┐                           │
│       └── Retriever ──┴─ 并行 (墙钟 localize_retrieve_ms) │
│       │                                           │
│       ▼  (失败 → agent_errors + 降级继续)          │
│  Patcher (直连 API → JSON → 宿主机落盘)            │
│       │                                           │
│       ▼                                           │
│  Verifier (run_sandbox_verification)              │
│       │                                           │
│   all_passed ──yes──► status=fixed                │
│       │ no                                        │
│       └──► git checkout 回滚 + feedback → Patcher  │
│            (max 3 轮)                             │
└──────────────────────────────────────────────────┘
        │
        ▼
Docker: tar → pip install (可选) → pytest --json-report
```

---

## 5. 三案例 E2E 结果

> 各 demo 在 git 中保持 **bug 状态**；跑完 repair 后需 `git checkout -- demo/<name>` 恢复。

| Demo | 错误类型 | 修复？ | 典型耗时 | 关键补丁 |
|------|----------|--------|----------|----------|
| `demo/calculator` | TypeError（str + int） | ✅ | ~25s | `int(a) + int(b)` |
| `demo/importer` | ImportError（`utils.helper`） | ✅ | ~21s | `from utils.helpers import greet` |
| `demo/logic_bug` | off-by-one | ✅ | ~24s | `range(1, n + 1)` |

### 5.1 参考耗时分解（calculator，并行后）

| 阶段 | 约耗时 |
|------|--------|
| parse_issue | <10ms |
| Localizer ∥ Retriever（墙钟） | ~16–18s |
| Patcher | ~5–21s（API 方差） |
| Verifier (Docker) | ~0.9s |
| **总计** | **~22–45s** |

---

## 6. 如何跑真实 E2E

### 6.1 前置条件

1. `cp .env.example .env`，填入 `DEEPSEEK_API_KEY`
2. Docker 运行中，镜像已构建：
   ```bash
   docker build -t repair-agent/python-repair:latest -f sandbox/Dockerfile.python sandbox/
   ```
3. `pip install -e ".[dev]"`

### 6.2 单案例命令

```bash
# 确认 bug 存在
pytest demo/calculator -q   # 预期有 failed

# 完整修复
python -m src.cli repair \
  --issue "TypeError: can only concatenate str (not 'int') to str at calculator.py:6 in add()" \
  --repo ./demo/calculator \
  --verbose

# 恢复 bug 状态（演示后必做）
git checkout -- demo/calculator
```

**importer：**

```bash
python -m src.cli repair \
  --issue "ModuleNotFoundError: No module named 'utils.helper' at app.py:3" \
  --repo ./demo/importer --verbose
```

**logic_bug：**

```bash
python -m src.cli repair \
  --issue "AssertionError: assert [1, 2] == [1, 2, 3] at sequence.py:8 in iota()" \
  --repo ./demo/logic_bug --verbose
```

### 6.3 一键演示脚本

```bash
bash demo/demo_repair.sh              # 默认 calculator
bash demo/demo_repair.sh all          # 依次跑 3 个 case（自动 git checkout 恢复）
SKIP_VERIFY=1 bash demo/demo_repair.sh calculator   # 无 Docker 时
```

### 6.4 成功标准

| 检查项 | 期望 |
|--------|------|
| CLI 输出 | `✅ 修复完成! 状态=fixed` |
| 沙箱 pytest | 相关用例通过 |
| 本地 pytest | demo 内全部通过（修复后、恢复前） |
| stderr | 各阶段 ms 输出；`localize_retrieve_ms` 为并行墙钟 |

---

## 7. 测试矩阵

| 测试文件 | 覆盖 |
|----------|------|
| `tests/test_e2e_repair.py` | 自愈 mock、无 Verifier、反馈格式 |
| `tests/test_orchestrator.py` | 解析、补丁应用、prompt 预读 |
| `tests/test_orchestrator_robustness.py` | Localizer 降级、Retriever 失败、沙箱 destroy |
| `tests/test_sandbox_manager.py` | PatchApplier、PythonTestRunner JSON |
| `tests/test_semantic_memory.py` | HF 离线、懒加载 |
| 全量 | **359 tests**，覆盖率 **74%** |

```bash
pytest tests/ -v
pytest tests/ -v --cov=agent_runtime --cov=src --cov-report=term-missing
ruff check agent_runtime src tests
ruff format --check agent_runtime src tests
```

---

## 8. 已知限制与后续（M7+）

| 项 | 状态 |
|----|------|
| Verifier 只跑 Retriever 返回的相关用例 | 可能显示 `1/1` 而非全量；完整确认需本地 `pytest` |
| 容器镜像预 pull / 连接池复用 | 延后（Day 29 计划项） |
| `src/cli.py` / `src/agents/verifier.py` 覆盖率偏低 | 单测以 Fake + mock 为主，CLI 靠 E2E 脚本验证 |
| 评测集 / 消融实验 | M7 计划（`docs/M7-M8-DAILY.md`） |

---

## 10. M6 复盘（M6D5 收尾）

### 10.1 代码与测试统计（`e49ef24` + M6D5 本地改动）

| 范围 | Python 行数（含空行/注释） | 说明 |
|------|---------------------------|------|
| `agent_runtime/` | **3,724** | Layer 1 运行时内核 |
| `src/` | **2,302** | Layer 2 多 Agent 修复系统 |
| **合计** | **6,026** | 超出原计划 ~3000 行（计划未计测试与 M3/M4 扩展模块） |
| 测试用例 | **359** | `pytest tests/ -v` 全绿 |
| 覆盖率 | **74%** | `--cov=agent_runtime --cov=src` |

> 原计划 M6 目标 ~3000 行指「核心手写路径」粗估；当前仓库含完整记忆/熔断/CLI 等 M1–M4 能力，行数更高属正常。

### 10.2 M1–M6 累计能力

| 里程碑 | 计划测试数 | 实际测试数 | 核心能力 | 状态 |
|:--:|:--:|:--:|------|:--:|
| M1 | 20 | ✓ | 控制循环 + 工具 + Config + Workspace | ✅ |
| M2 | 40 | ✓ | 6 工具 + 闸口 + Token 预算 + Dry-Run | ✅ |
| M3 | 55 | ✓ | 三层记忆 + Checkpoint + 持久化 + 安全 | ✅ |
| M4 | 70 | ✓ | 语义记忆 + 配额 + 熔断 + Replay | ✅ |
| M5 | 85 | ✓ | 4 Agent + Blackboard + ToolGateway + Skill | ✅ |
| M6 | 95 | **359** | Docker Harness + Verifier + 自愈 + 3 Demo + 鲁棒性 | ✅ |

### 10.3 Day 30 验收清单

| 验收项 | 状态 |
|--------|------|
| Docker 沙箱完整运作 | ✅ |
| Verifier 容器内构建 + 测试 | ✅ |
| 自愈循环最多 3 轮 | ✅ |
| 完整闭环（定位→检索→补丁→验证→反馈） | ✅ |
| Localizer + Retriever 并行 | ✅ PR #66 |
| 超时与降级保护 | ✅ PR #68 |
| `pytest tests/ -v --cov` 全绿，90+ tests | ✅ 359 / 74% |
| `ruff check` + `ruff format` 零 warning | ✅ M6D5 |
| `demo/demo_repair.sh` 可复现 | ✅ M6D5 |
| `git tag m6-done` | ✅ M6D5 |

### 10.4 已知问题（带入 M7）

1. **Verifier 用例范围**：沙箱可能只跑 Retriever 点名的用例，全量需本地 `pytest`。
2. **沙箱性能**：镜像预 pull、连接池未做；冷启动 ~1s/次。
3. **覆盖率缺口**：`src/cli.py`、`verifier.py`、真实 Docker 路径偏低。
4. **CLI 退出码**：`repair` 失败时仍返回 0，脚本靠输出文本判断。

### 10.5 M7 方向

- 评测集与 `src/eval/runner` 消融实验（见 `docs/M7-M8-DAILY.md`）
- 修复成功率 / 耗时 / retry 次数批量统计
- CLI 退出码与结构化 JSON 输出（便于 CI）
- 可选：沙箱连接池、镜像预热

---

## 11. Layer 2 能力矩阵（M6 完成态）

| 组件 | 职责 | 入口 | LLM？ |
|------|------|------|-------|
| **CLI** | `repair` 命令、加载 `.env`、组装 Agent | `python -m src.cli` | 否 |
| **Orchestrator** | 解析 Issue、调度、自愈、超时 | `src/orchestrator.py` | 否 |
| **Localizer** | 堆栈/AST 定位 → `SuspectLocation` | `src/agents/localizer.py` | 是（tool loop） |
| **Retriever** | search/find_test → `RetrievedContext` | `src/agents/retriever.py` | 是（tool loop） |
| **Patcher** | 生成 `CandidatePatch` JSON | `src/agents/patcher.py` | 是（Orchestrator 直连 API） |
| **Verifier** | 容器内 pytest | `run_sandbox_verification()` | 否（直连 harness） |
| **SandboxManager** | Docker 生命周期 + tar 传文件 | `src/harness/sandbox_manager.py` | 否 |
| **RepairState** | 流水线共享状态 + `agent_errors` | `src/state.py` | 否 |
| **ToolGateway** | Agent 工具权限隔离 | `src/middleware.py` | 否 |
| **Demo ×3** | TypeError / ImportError / logic bug | `demo/{calculator,importer,logic_bug}/` | — |

**数据流：** `Issue` → `RepairPlan` → `(SuspectLocation ∥ RetrievedContext)` → `CandidatePatch` → `VerificationResult` → `fixed | failed | exhausted`

---

## 12. 相关文档

- 总体规划：`docs/DEVELOPMENT_PLAN_ALL.md`
- M5 多 Agent：`docs/M5_GUIDE.md`
- M6 每日计划：`docs/M5-M6-DAILY.md`（Day 26–30）
- 项目约定：`CLAUDE.md`
