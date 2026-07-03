# M6 GUIDE — Docker 沙箱 + Verifier + E2E 闭环

> M6 在 M5 多 Agent 流水线之上，补齐 **Docker 容器验证** 与 **真实 API 端到端修复**。PR [#65](https://github.com/changsheng1224/FixLoop/pull/65)（squash merge `477e43f`，2026-07-03）在 M6D3 基础上做了 E2E 加固、性能观测与项目规范落地。

---

## 1. PR #65 概览

| 项 | 内容 |
|----|------|
| **PR** | [#65 — M6D3: E2E repair pipeline hardening + timing + agent rules](https://github.com/changsheng1224/FixLoop/pull/65) |
| **合并 commit** | `477e43f` |
| **变更规模** | 26 文件，+1144 / −245 行 |
| **测试** | `pytest tests/` — **346 passed** |
| **真实 E2E** | `demo/calculator` — `status=fixed`，本地 pytest **4/4** |

### 1.1 合并前已包含的子 commit（squash 前）

1. `perf`: Localizer / Retriever 可选本地 Ollama（`light_client`）
2. `feat`: Orchestrator 各 Agent `node_timings` 埋点
3. `feat`: stderr 实时阶段耗时输出（`[HH:MM:SS] Localizer 完成: …ms`）
4. `feat(M6D3)`: E2E 加固、Cursor 规则、`AGENTS.md`

---

## 2. M6 相对 M5：新增能力

| 能力 | M5 状态 | M6 新增 |
|------|---------|---------|
| 容器执行 | 无 | **Docker 沙箱**（`SandboxManager` + tar 传文件） |
| 验证 Agent | 预留 | **Verifier**（`sandbox_build` / `sandbox_test` / `sandbox_verify`） |
| 自愈闭环 | 设计 | **Patcher ↔ Verifier** 最多 3 轮，失败回滚 + 反馈 |
| Demo | 无 | **`demo/calculator/`** 故意 TypeError bug |
| E2E 单测 | 无 | **`tests/test_e2e_repair.py`** 边界场景（Fake + mock Verifier） |

---

## 3. PR #65 核心改动（按模块）

### 3.1 Orchestrator — 编排加固与性能路径

**文件：** `src/orchestrator.py`

| 改动 | 说明 |
|------|------|
| **Patcher 直连 API** | `_run_patcher()` 绕过 Agent tool loop，一次 `model_client.complete()` + JSON 解析，减少 DeepSeek 不遵守 tool 格式的问题 |
| **Verifier 直连 harness** | `_run_verifier()` 调用 `run_sandbox_verification()`，不再走 Verifier LLM loop |
| **补丁落盘** | `apply_patch_to_text()` / `_apply_patches_on_disk()` 支持 `original_lines` 与 unified `diff`，strip 匹配 + 保留缩进 + 忽略行内注释 |
| **repo 根路径** | `_repo_root` 优先 `--repo` / `workspace.cwd`，不再误用 git 顶层目录 |
| **嫌疑文件预读** | `_patcher_prompt()` 嵌入真实代码片段，跳过空 `file_path`，仅 `is_file()` 时读取 |
| **耗时** | `state.node_timings` + stderr 实时输出；Patcher/Verifier 记录 `*_internal` 子阶段 |
| **自愈** | 验证失败 → `git checkout` 回滚 → `_build_feedback()` 注入下一轮 Patcher |

### 3.2 Docker 沙箱 — 生命周期修复

**文件：** `src/harness/sandbox_manager.py`、`sandbox/entrypoint.sh`、`sandbox/Dockerfile.python`

| 问题 | 修复 |
|------|------|
| 容器启动即退出 | `entrypoint.sh` 原先 `cd /code` 但镜像无 `/code` → 改为 `mkdir -p /code`；Dockerfile 预建 `/code` |
| `put_archive` RWLayer nil | 容器命令改为 `entrypoint=""` + `sleep infinity`，文件传完再经 `/entrypoint.sh` 执行 build/test |
| Windows bind mount 慢 | 继续用 tar 流式传文件进容器 |

### 3.3 Verifier / pytest — 假通过修复

**文件：** `src/harness/python_runner.py`、`src/tools/sandbox_tools.py`

| 问题 | 修复 |
|------|------|
| `0/0 通过` 仍标 `fixed` | `all_passed` 要求 `total_tests > 0` |
| 默认跑 `tests/` 目录 | 无 `tests/` 的 demo 改为默认 `pytest /code`（`.`） |
| JSON 报告解析失败 | `_read_report()` 去掉无效 shell 重定向（`exec_run` 不走 shell），直接 `cat /code/.report.json` |
| 报告缺失仍当通过 | 降级路径改为 `all_passed=False` |

**新增：** `run_sandbox_verification()` — Orchestrator 与 `sandbox_verify` tool 共用入口。

### 3.4 CLI — 修复目标目录

**文件：** `src/cli.py`

- 各 Agent 工厂传入 `cwd=<repo 绝对路径>`
- `--verbose` 打印各阶段 `node_timings`（prompt / model / tool 分项）
- Verifier 接入前检测 Docker `ping`

### 3.5 Layer 1 运行时增强

**文件：** `agent_runtime/agent_loop.py`、`runtime.py`、`providers/clients.py`、`prompt_prefix.py`

- Agent loop 逐步耗时（tool / parse / model）
- `light_client` 支持（Localizer/Retriever 可走本地 Ollama）
- Anthropic 兼容 client 增强（重试、latency 统计等）

### 3.6 Prompt 精调

**文件：** `src/prompts/localizer.txt`、`retriever.txt`、`patcher.txt`、`verifier.txt`

- Localizer：聚焦 `ast_parse` / `stack_parse` / `read_file`，强调必须先调工具
- Retriever：聚焦 search / find_test / git_blame
- Patcher：**只输出 JSON**，不再要求调 write/patch tool（由 Orchestrator 落盘）
- 单测同步：`tests/test_prompts_m5.py`

### 3.7 项目 AI 协作规范

| 文件 | 作用 |
|------|------|
| `CLAUDE.md` | 权威项目约定（架构、Git、测试、代理） |
| `.cursor/rules/fixloop-project-conventions.mdc` | Cursor 始终注入规则 |
| `AGENTS.md` | 其他 Agent 工具入口，指向 `CLAUDE.md` |

---

## 4. 端到端架构（PR #65 后）

```
Issue + --repo ./demo/calculator
        │
        ▼
┌─────────────────────────────────────────┐
│ Orchestrator                             │
│  parse_issue → Localizer → Retriever     │
│       │                                  │
│       ▼                                  │
│  Patcher (直连 API → JSON → 宿主机落盘)   │
│       │                                  │
│       ▼                                  │
│  Verifier (直连 run_sandbox_verification)│
│       │                                  │
│   all_passed ──yes──► status=fixed       │
│       │ no                             │
│       └──► 回滚 + feedback → Patcher     │
│            (max 3 轮)                    │
└─────────────────────────────────────────┘
        │
        ▼
Docker: tar 传 repo → pytest --json-report → 解析 VerificationResult
```

---

## 5. 如何跑真实 E2E

### 5.1 前置条件

- `.env` 中配置 `DEEPSEEK_API_KEY`（或所用模型 API）
- Docker 运行中，镜像 `repair-agent/python-repair:latest` 已构建
- `demo/calculator` 处于 bug 状态（`test_add_str` 预期失败）

### 5.2 命令

```bash
# 确认 bug 存在
pytest demo/calculator -q   # 预期 1 failed

# 完整修复
python -m src.cli repair \
  --issue "TypeError: can only concatenate str (not 'int') to str at calculator.py:6 in add()" \
  --repo ./demo/calculator \
  --verbose
```

### 5.3 成功标准

| 检查项 | 期望 |
|--------|------|
| CLI 输出 | `✅ 修复完成! 状态=fixed` |
| `calculator.py` | 含类型转换修复（如 `int(a) + int(b)`） |
| 本地 pytest | 4/4 passed |
| stderr 耗时 | Localizer / Retriever / Patcher / Verifier 均有 ms 输出 |

### 5.4 参考耗时（单次实测）

| 阶段 | 约耗时 |
|------|--------|
| Localizer | ~11–14s |
| Retriever | ~13–21s |
| Patcher | ~7–15s |
| Verifier (Docker) | ~0.8–1.7s |
| **总计** | **~40–70s** |

---

## 6. 测试矩阵

| 测试文件 | 覆盖 |
|----------|------|
| `tests/test_e2e_repair.py` | 自愈 mock、无 Verifier 降级、反馈格式 |
| `tests/test_orchestrator.py` | `apply_patch_to_text` diff/缩进/注释 |
| `tests/test_sandbox_manager.py` | PatchApplier、PythonTestRunner JSON 解析 |
| `tests/test_prompts_m5.py` | 更新后的 Prompt 约束 |
| 全量 | **346 tests** |

---

## 7. 变更文件清单（PR #65）

```
.cursor/rules/fixloop-project-conventions.mdc   # 新增
AGENTS.md                                       # 新增
agent_runtime/agent_loop.py
agent_runtime/prompt_prefix.py
agent_runtime/providers/clients.py
agent_runtime/runtime.py
sandbox/Dockerfile.python
sandbox/entrypoint.sh
src/agents/{localizer,retriever,patcher,verifier}.py
src/cli.py
src/harness/python_runner.py
src/harness/sandbox_manager.py
src/orchestrator.py
src/prompts/{localizer,retriever,patcher,verifier}.txt
src/tools/sandbox_tools.py
tests/test_e2e_repair.py
tests/test_integration.py
tests/test_orchestrator.py
tests/test_prompts_m5.py
tests/test_tools.py
```

---

## 8. 已知限制与后续（M7+）

| 项 | 状态 |
|----|------|
| Verifier 只跑 Retriever 返回的相关用例 | 可能显示 `1/1` 而非 `4/4`；全量需本地 pytest 确认 |
| `demo/importer/`、`demo/logic_bug/` | M6 Day 29 计划，尚未创建 |
| Localizer + Retriever 并行 | 计划中的 `asyncio.gather`，当前仍串行 |
| 评测集 / 消融实验 | M7 计划（`docs/M7-M8-DAILY.md`） |

---

## 9. 相关文档

- 总体规划：`docs/DEVELOPMENT_PLAN_ALL.md`
- M5 多 Agent：`docs/M5_GUIDE.md`
- M6 每日计划：`docs/M5-M6-DAILY.md`（Day 26–30）
- 项目约定：`CLAUDE.md`
