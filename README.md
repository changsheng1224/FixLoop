# FixLoop

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![pytest](https://img.shields.io/badge/tests-pytest-green.svg)](https://docs.pytest.org/)
[![ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![Docker](https://img.shields.io/badge/sandbox-Docker-2496ED.svg)](https://www.docker.com/)

**从零构建的 Agent 代码修复系统**：手写 Agent 运行时（Layer 1）+ 受治理的修复流水线（Layer 2），覆盖工具执行、上下文与记忆、Docker 沙箱验证、Canonical Trace 和可复现评测。

## 目录

- [架构概览](#架构概览)
- [为什么与众不同](#为什么与众不同)
- [快速开始](#快速开始)
- [Demo 脚本](#demo-脚本)
- [使用示例](#使用示例)
- [评测结果](#评测结果)
- [代码规模](#代码规模)
- [项目结构](#项目结构)
- [依赖与环境](#依赖与环境)
- [开发与测试](#开发与测试)

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 2  Multi-Agent Repair (src/)                          │
│  Issue → Intent / Rule Seed → Patcher → Critic               │
│         → Verifier (pytest / Docker) → 反馈、回滚、重试       │
└───────────────────────────┬─────────────────────────────────┘
                            │ Agent.ask() / model_client
┌───────────────────────────▼─────────────────────────────────┐
│ Layer 1  Agent Runtime (agent_runtime/)                     │
│  CLI → AgentLoop → ContextManager → Provider → ToolExecutor │
│  + Memory / Checkpoint / Trace / CircuitBreaker             │
└─────────────────────────────────────────────────────────────┘
```

**Layer 1** 是通用 Agent 内核，不依赖 LangChain/LangGraph 等 LLM 编排框架。

**Layer 2** 当前主路径是 Patcher 工具环、轻量 Critic 和独立 Verifier；Localizer/Retriever 保留为规则种子、状态模型和历史兼容语义，不应描述为当前主路径中的两个独立 LLM Agent。

Repair 默认使用证据驱动的小型任务 DAG：最多 8 个节点、两路只读探索、两次静止点重规划，主 Agent 串行修改。Plan journal、工具收据及文件版本共同支持在途恢复；未知写入不会自动重放。使用方法和恢复边界见 [Plan DAG 指南](docs/PLAN_DAG.md)。

## 为什么与众不同

与「LangChain 模板 + 一个 ReAct Agent」的常见做法相比：

1. **执行与裁决分离**：Patcher 负责搜读改测，Critic 负责提交前廉价检查，Verifier 负责独立测试判定；Orchestrator 纯 Python 调度，不把编排决策交给 LLM。
2. **运行时自己写**：控制循环、工具闸口、Token 预算、Checkpoint、Trace 均为标准库 + 少量依赖实现，可逐行审计。
3. **可诊断的评测闭环**：Case Runner、Single-Agent 基线、消融实验、Canonical Trace 和 `regression_check` 回归门禁共同记录修复结果与失败归因。

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

预期：`stderr` 打印 Patcher / Critic / Verifier 阶段日志；成功时 `status=fixed`，`demo/calculator` 下 pytest 通过。

无 Docker 时仍可用本地 **pytest verify**（默认开启）；跳过验证：

```bash
python -m src.cli repair --issue "..." --repo demo/calculator --skip-verify
```

**一键演示脚本（含修复前/后 pytest）：**

```bash
bash demo/demo_repair.sh calculator
# SKIP_VERIFY=1 bash demo/demo_repair.sh   # 无 Docker
```

### 4. GitHub 仓库 + 问题描述

安装 CLI 与容器验证依赖，配置 `DEEPSEEK_API_KEY`，并构建上述沙箱镜像：

```bash
pip install -e ".[sandbox]"
fixloop repair --repo https://github.com/owner/project --issue "复现步骤……实际行为……预期行为……"
```

`--repo` 也接受 `owner/project`、`git@github.com:owner/project.git` 或本地目录。
已有本地目录优先按路径解释；GitHub 输入会克隆到独立目录并固定基线 commit。
Git 复用本机已有认证和代理配置，不在 URL 中填写 token。私有仓库可使用已配置的 SSH 身份。

长问题和指定版本：

```bash
fixloop repair --repo owner/project --ref v1.2.0 --issue-file issue.md
fixloop repair --repo owner/project --issue "……" --output ../repair-result
```

`issue.md` 使用 UTF-8，可包含堆栈、复现步骤、预期结果和修改约束。
`--issue` 与 `--issue-file` 二选一；`--ref` 支持分支、tag 或 commit SHA。
`--output` 必须是新的目录，默认 `.fixloop/runs/<id>/`。结果目录包括：

```text
result.json   # 状态、基线 SHA、验证结果、错误分类和进度记录
report.md     # 可读报告
patch.diff    # 最终工作树修改，含新增/删除文件和 Git 二进制补丁
repo/         # GitHub 输入的独立工作目录
```

首期 GitHub 入口支持 Python/pytest 项目。默认要求 Docker 验证；容器或镜像不可用时
报告 `verification_environment_failed`。项目依赖必须在现有验证环境中可用，自动准备
任意仓库依赖属于后续范围。`--execution-tier` 描述独立 Verifier 的执行层，Patcher
继续使用现有运行时及工具权限机制。

显式 `--execution-tier host` 使用本地验证；`--execution-tier static` 只做静态检查；
`--skip-verify` 生成待验证补丁。静态检查、零测试和跳过验证不会在结果报告中显示为
已经验证的修复。测试通过会注明实际验证范围，不代表整个仓库的全量测试通过。

应用补丁前，在对应基线版本的干净仓库中检查：

```bash
git apply --check /path/to/result/patch.diff
git apply /path/to/result/patch.diff
```

本地目录保留原有原地修复方式，补丁以启动时工作树为基线，排除预先存在的未跟踪
文件、`.agent`、`.fixloop`、缓存和 `.env`。非 Git 目录支持文本补丁导出。
Ctrl+C 会协作取消并保留报告、工作目录和可导出的已有修改。
退出码：`0` 有可交付补丁（需检查报告是否已验证）、`1` 修复或导出失败、
`2` 输入/配置/准备失败、`3` 超时、`130` 用户取消。

详细输入、结果与失败边界见 [CLI 使用指南](docs/GITHUB_REPAIR_CLI.md)。

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

以下是历史 M7 消融快照（`full` + `single` × 10 Case × 3 次 = **60 runs**）。它用于说明评测格式，不代表当前提交的实时基线；更新简历或发布材料前应按当前配置重新运行。

| 变体 | Fix Rate | 平均耗时 | 平均 Token | Patch 精度 |
|------|----------|----------|------------|------------|
| **full（多角色编排）** | **30/30 (100%)** | 31.8s | 5182 | 1.22 |
| **single**（Baseline） | 29/30 (96.7%) | 19.7s | 2581 | 0.94 |
| **合计** | 59/60 (98.3%) | 25.7s | 3882 | 1.08 |

要点：

- 历史数据中 full 变体 **30/30 零失败**；Single 有 1 次偶发「未产出补丁」。
- full 用约 **2× Token** 换取更高通过率与更小补丁（Case 为 1–3 文件的微型 repo，差距未拉大到 15pp，详见本地 `eval_results/final_report.md`）。
- **0%** 引入回归（`introduced_regression`）。

Case 覆盖：TypeError、ImportError、AttributeError、logic_error、config_error、composite（见 `src/eval/cases/README.md`）。

## 代码规模

统计日期：**2026-08-09**。统计对象为 Python 源文件；物理行包含空行和注释，非注释代码行排除了空行及以 `#` 开头的注释行。

| 范围 | 文件数 | 物理行 | 非空行 | 非注释代码行 |
|---|---:|---:|---:|---:|
| `agent_runtime/`（Layer 1） | 155 | 35,411 | 30,840 | 30,289 |
| `src/`（Layer 2） | 168 | 29,737 | 25,838 | 25,543 |
| **生产源码合计** | **323** | **65,148** | **56,678** | **55,832** |

统计排除了 `src/eval/cases/**` 中的评测仓库快照、`__pycache__`、缓存、`artifacts/` 和临时目录。测试代码单独统计为 227 个 Python 文件、约 2,183 个 `test_*` 函数，不计入生产源码行数。

## 项目结构

```
FixLoop/
├── agent_runtime/          # Layer 1：Agent 内核（loop / tools / memory / providers）
├── src/
│   ├── agents/             # Patcher / Verifier 工厂
│   ├── orchestrator.py     # Issue、规则种子与修复阶段调度
│   ├── repair/             # Critic、反馈、回滚、验证和状态协作
│   ├── eval/               # Case 库、Runner、Baseline、Ablation、Metrics
│   ├── harness/            # Docker 沙箱 + pytest runner
│   └── cli.py              # repair / eval / ablation 命令
├── sandbox/                # Docker 镜像定义
├── demo/                   # calculator / importer / logic_bug 演示项目
├── tests/                  # 227 个测试文件，约 2,183 个 test 函数
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

历史代码终审记录见 [`docs/CODE_REVIEW.md`](docs/CODE_REVIEW.md)。其中的测试数量和覆盖率是历史快照，不应直接作为当前版本指标；请以最近一次完整测试和 coverage 输出为准。

分支与 PR 流程见 [`CLAUDE.md`](CLAUDE.md)。架构与设计决策见 [`ARCHITECTURE.md`](ARCHITECTURE.md)、[`docs/design-decisions.md`](docs/design-decisions.md)。Layer 1 模块导读见 [`LAYER1_GUIDE.md`](LAYER1_GUIDE.md)。

---

**License:** MIT
