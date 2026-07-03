# FixLoop

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![pytest](https://img.shields.io/badge/tests-pytest-green.svg)](https://docs.pytest.org/)
[![ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![Docker](https://img.shields.io/badge/sandbox-Docker-2496ED.svg)](https://www.docker.com/)

**从零构建的多 Agent 代码修复系统**：手写 Agent 运行时（Layer 1）+ 分工修复流水线（Layer 2），含 Docker 沙箱验证与 10 Case 消融评测。

## 目录

- [架构概览](#架构概览)
- [为什么与众不同](#为什么与众不同)
- [快速开始](#快速开始)
- [Demo 脚本](#demo-脚本)
- [使用示例](#使用示例)
- [评测结果](#评测结果)
- [项目结构](#项目结构)
- [依赖与环境](#依赖与环境)
- [开发与测试](#开发与测试)

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 2  Multi-Agent Repair (src/)                          │
│  Issue → Orchestrator → Localizer ∥ Retriever → Patcher     │
│         → Verifier (pytest / Docker) → 失败则回滚重试        │
└───────────────────────────┬─────────────────────────────────┘
                            │ Agent.ask() / model_client
┌───────────────────────────▼─────────────────────────────────┐
│ Layer 1  Agent Runtime (agent_runtime/)                     │
│  CLI → AgentLoop → ContextManager → Provider → ToolExecutor │
│  + Memory / Checkpoint / Trace / CircuitBreaker             │
└─────────────────────────────────────────────────────────────┘
```

**Layer 1** 是通用 Agent 内核（~1900 行，零 LLM 框架依赖）。  
**Layer 2** 在之上实现 Localizer / Retriever / Patcher / Verifier 分工与评测体系。

## 为什么与众不同

与「LangChain 模板 + 一个 ReAct Agent」的常见做法相比：

1. **真分工，非单 Agent 换皮**：定位、检索、补丁、验证由不同 Agent 与 Prompt 约束；Orchestrator 纯 Python 调度，不嵌 LLM。
2. **运行时自己写**：控制循环、工具闸口、Token 预算、Checkpoint、Trace 均为标准库 + 少量依赖实现，可逐行审计。
3. **可复现的评测闭环**：10 个微型 Case、Single-Agent 基线、消融实验、`regression_check` 回归门禁，Fix Rate 有数据支撑。

## 快速开始

### 1. 安装

```bash
git clone git@github.com:changsheng1224/FixLoop.git
cd FixLoop
pip install -e ".[dev]"
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

### 2. Layer 1：Agent 运行时（无需 Docker）

```bash
python -m agent_runtime "列出当前目录下的 Python 文件"
```

预期：Agent 调用 `list_files` / `read_file` 等工具后返回 `<final>...</final>` 文本结论。

进入 REPL：直接运行 `python -m agent_runtime`（无参数）。

### 3. Layer 2：修复 demo 项目

**可选 — 构建沙箱镜像（Docker 验证时需要）：**

```bash
docker build -t repair-agent/python-repair:latest -f sandbox/Dockerfile.python sandbox/
```

**运行修复（需 API Key）：**

```bash
python -m src.cli repair \
  --issue "TypeError: can only concatenate str (not 'int') to str at calculator.py:6 in add()" \
  --repo demo/calculator \
  --verbose
```

预期：`stderr` 打印 Localizer / Patcher / Verifier 阶段日志；成功时 `status=fixed`，`demo/calculator` 下 pytest 通过。

无 Docker 时仍可用本地 **pytest verify**（默认开启）；跳过验证：

```bash
python -m src.cli repair --issue "..." --repo demo/calculator --skip-verify
```

**一键演示脚本（含修复前/后 pytest）：**

```bash
bash demo/demo_repair.sh calculator
# SKIP_VERIFY=1 bash demo/demo_repair.sh   # 无 Docker
```

## Demo 脚本

M8 录屏 / 面试演示用三段式脚本（仓库根目录、Git Bash / Linux）：

| 脚本 | 内容 | 前置 |
|------|------|------|
| `demo/demo_1_repair.sh` | calculator 完整修复：issue → pytest 红 → repair → diff → 绿 | API Key + pytest verify（或 `SKIP_VERIFY=1`） |
| `demo/demo_2_self_healing.sh` | case_006 自愈：Verifier 失败 → 回滚 → feedback → 重试 | 同上；需 verify 才展示 retry |
| `demo/demo_3_ablation.sh` | 3 变体 × 3 Case 消融对比表 | 默认 `--fake` 无需 API；`USE_API=1` 走真实 API |

```bash
bash demo/demo_1_repair.sh
bash demo/demo_2_self_healing.sh
bash demo/demo_3_ablation.sh              # ~10s，fake
USE_API=1 bash demo/demo_3_ablation.sh    # 真实 API，数分钟
```

另有聚合脚本 `demo/demo_repair.sh`（calculator / importer / logic_bug）。演示视频 / GIF 可放 GitHub Release 外链。

## 使用示例

### Layer 1

```bash
# 单次问答
python -m agent_runtime "读取 README 第一段并总结"

# Dry-run（不写盘）
python -m agent_runtime --dry-run "修改 calculator.py"

# 恢复上次会话
python -m agent_runtime --resume
```

### Layer 2

```bash
# 单 Case 评测（Fake，无需 API）
python -m src.cli eval --case case_001 --fake --markdown

# 全量评测（真实 API，默认 pytest verify）
python -m src.cli eval --all --output eval_results/run1 --markdown

# 消融实验：full vs single，各 3 次重复
python -m src.cli ablation --all --variant full --variant single \
  --repetitions 3 --output eval_results/ablation --markdown --verbose

# 回归门禁（对比两次报告）
python -m src.eval.regression_check \
  --current eval_results/run1/eval_report.json \
  --baseline eval_results/baseline_report.json
```

## 评测结果

M7 正式消融（`full` + `single` × 10 Case × 3 次 = **60 runs**，pytest verify 开启）：

| 变体 | Fix Rate | 平均耗时 | 平均 Token | Patch 精度 |
|------|----------|----------|------------|------------|
| **full**（4-Agent） | **30/30 (100%)** | 31.8s | 5182 | 1.22 |
| **single**（Baseline） | 29/30 (96.7%) | 19.7s | 2581 | 0.94 |
| **合计** | 59/60 (98.3%) | 25.7s | 3882 | 1.08 |

要点：

- Multi-Agent **30/30 零失败**；Single 有 1 次偶发「未产出补丁」。
- full 用约 **2× Token** 换取更高通过率与更小补丁（Case 为 1–3 文件的微型 repo，差距未拉大到 15pp，详见本地 `eval_results/final_report.md`）。
- **0%** 引入回归（`introduced_regression`）。

Case 覆盖：TypeError、ImportError、AttributeError、logic_error、config_error、composite（见 `src/eval/cases/README.md`）。

## 项目结构

```
FixLoop/
├── agent_runtime/          # Layer 1：Agent 内核（loop / tools / memory / providers）
├── src/
│   ├── agents/             # Localizer / Retriever / Patcher / Verifier 工厂
│   ├── orchestrator.py     # 修复流水线调度
│   ├── eval/               # Case 库、Runner、Baseline、Ablation、Metrics
│   ├── harness/            # Docker 沙箱 + pytest runner
│   └── cli.py              # repair / eval / ablation 命令
├── sandbox/                # Docker 镜像定义
├── demo/                   # calculator / importer / logic_bug 演示项目
├── tests/                  # 474+ pytest
└── docs/                   # 里程碑设计与日报
```

## 依赖与环境

| 类别 | 要求 |
|------|------|
| Python | **3.11+** |
| 核心依赖 | `pydantic`, `tiktoken`, `sentence-transformers`, `pyyaml` |
| 可选 | Docker（Verifier 沙箱）、DeepSeek API Key（真实修复/评测） |
| 开发 | `pytest`, `pytest-cov`, `ruff`（`pip install -e ".[dev]"`） |

环境变量见 [`.env.example`](.env.example)：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 等。

## 开发与测试

```bash
# 全量测试
pytest tests/ -v

# Lint（与 CI test.yml 一致）
ruff check agent_runtime src tests
ruff format --check agent_runtime src tests

# CI 评测门禁（本地）
python -m src.eval.runner --ci
python -m src.eval.regression_check \
  --current eval_results/ci/eval_report.json \
  --baseline src/eval/ci_baseline_report.json
```

GitHub Actions 配置在 [`.github/workflows/`](.github/workflows/)（**默认不自动触发**，仅 `workflow_dispatch` 或本地命令）。启用方法见 [`.github/workflows/README.md`](.github/workflows/README.md)。

M8D4 代码终审见 [`docs/CODE_REVIEW.md`](docs/CODE_REVIEW.md)（覆盖率 80%，475 tests）。

分支与 PR 流程见 [`CLAUDE.md`](CLAUDE.md)。架构与设计决策见 [`ARCHITECTURE.md`](ARCHITECTURE.md)、[`docs/design-decisions.md`](docs/design-decisions.md)。Layer 1 模块导读见 [`LAYER1_GUIDE.md`](LAYER1_GUIDE.md)。

---

**License:** MIT
