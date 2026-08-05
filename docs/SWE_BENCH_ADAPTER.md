# SWE-bench Lite Benchmark Adapter v1

> 将 SWE-bench Lite 实例接入 FixLoop：数据加载 → 仓库准备 → `repair` → Patch 导出 → 官方 Harness 判分。  
> **代码权威**：`src/benchmark/swebench/`。计划勾选见 `docs/2026-08-03-to-08-09-enhancement-plan.md`（8 月 8 日下午）。  
> **改码约束**：`docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`。**5 题失败与证据**：`docs/SWE_BENCH_LITE_DEV5_FAILURES.md`。

---

## 1. 范围

| 做 | 不做 |
|----|------|
| Adapter v1 + 固定 5 个 Lite 开发实例 | 把 Lite 分数当最终成绩 |
| Manifest / predictions.jsonl / 失败归因 | 强制把 `swebench` 打进核心依赖 |
| `--fake` / `--dry-run` 可重复烟雾 | 本仓库 CI 拉全量评测镜像 |
| 可选调用官方 `swebench.harness.run_evaluation` | 替你保证本机 120GB 磁盘 |

开发实例 ID（冻结，不因分数更换）见 `DEV_INSTANCE_IDS`：

- `astropy__astropy-12907`
- `django__django-11099`
- `matplotlib__matplotlib-23964`
- `pylint-dev__pylint-6506`
- `sympy__sympy-20590`

---

## 2. 安装与资源检查

### 2.0 P0：Windows → WSL Harness

官方 harness **不能**在 Windows 本机 Python 跑。Adapter 支持：

| 后端 | 何时 |
|------|------|
| `native` | Linux 本机已装 `swebench` |
| `wsl` | Windows 下经 `wsl -d <Ubuntu>` 调 `python3 -m swebench.harness.run_evaluation` |
| `auto` | 优先 native，否则 WSL |

### 2.1 预构建评测镜像（推荐，可复用）

官方 harness 会为每个 instance 构建/拉取 Docker 镜像；首次很慢。先预构建，后续 `run_evaluation` **自动跳过已有镜像**。

```powershell
# 仅 django（E1 smoke）
python -m src.benchmark.swebench --prepare-images --django-smoke

# 固定 DEV 5 题
python -m src.benchmark.swebench --prepare-images

# 或 WSL 内直接跑脚本
wsl -d Ubuntu -- bash /mnt/c/Users/haoyu/Documents/FixLoop/scripts/swebench_prepare_images.sh --django-only
wsl -d Ubuntu -- bash /mnt/c/Users/haoyu/Documents/FixLoop/scripts/swebench_prepare_images.sh --dev5
```

查看已缓存镜像：

```powershell
python -m src.benchmark.swebench --list-images
```

**复用注意**：不要执行 `docker system prune -a`（会删掉 `sweb.*` 评测镜像）。仅当 Dockerfile/依赖变更时才加 `--force-rebuild-images`。

```powershell
wsl --install -d Ubuntu
# 初次进入创建用户后：
wsl -d Ubuntu -- bash /mnt/c/Users/haoyu/Documents/FixLoop/scripts/swebench_wsl_setup.sh
```

探测：

```bash
python -m src.benchmark.swebench --probe-wsl
```

P0 三步（针对已有 live 产物）：

```bash
# 1) binary-safe 重导出（消除 UTF-8 假阴性）
python -m src.benchmark.swebench --reexport \
  --output-dir artifacts/swebench_lite_dev_live \
  --work-root artifacts/swebench_repos

# 2) 只评有 patch 的 django（冒烟）
python -m src.benchmark.swebench --django-smoke \
  --output-dir artifacts/swebench_lite_dev_live \
  --harness-backend wsl

# 3) 或显式 harness-only
python -m src.benchmark.swebench --harness-only \
  --instance-ids django__django-11099 \
  --output-dir artifacts/swebench_lite_dev_live \
  --harness-backend auto
```

环境变量：`FIXLOOP_WSL_DISTRO=Ubuntu`。

### 2.1 可选依赖

```bash
pip install datasets          # 从 HuggingFace 拉 Lite
pip install swebench          # 官方 Harness（或 git clone + pip install -e .）
# Docker Desktop：建议 ≥16GB RAM，镜像缓存可达数十～百 GB
```

`pyproject.toml` extras：`swebench = ["datasets", "swebench"]`（若已声明）。

### 2.2 冒烟（不装 harness）

仓库已带本地 fixture（5 条）：

```bash
python -m src.benchmark.swebench --dry-run \
  --instances-jsonl src/benchmark/swebench/fixtures/lite_dev5.jsonl \
  --output-dir artifacts/swebench_lite_dev
```

### 2.3 Fake 端到端（无 API，需本地预置 repo 目录）

```bash
# 为每个 instance_id 在 work-root 下准备 git 仓库后：
python -m src.benchmark.swebench --fake --skip-clone \
  --instances-jsonl src/benchmark/swebench/fixtures/lite_dev5.jsonl \
  --work-root artifacts/swebench_repos \
  --output-dir artifacts/swebench_lite_dev
```

### 2.4 真跑 Agent + 官方判分

```bash
# 需 API Key + 能 clone GitHub
# 流水线：FixLoop verify 通过 → 才写入 harness 评测集 → 可选 --run-harness
# 默认会跑 verify；仅调试可加 --skip-verify（此时默认禁止进官方 harness，除非 --allow-unverified-harness）
python -m src.benchmark.swebench \
  --provider anthropic_compat \
  --model-name fixloop-deepseek \
  --output-dir artifacts/swebench_lite_dev_live \
  --work-root artifacts/swebench_repos \
  --repair-timeout-s 900 \
  --run-harness
```

`predictions.jsonl` 含 `verified` 字段；`predictions.harness.jsonl` 默认只保留 `verified=true` 且非空 patch。
官方 Harness **需要 Linux/WSL/Modal**（依赖 Unix `resource` 模块；Windows 本机无法直接 `import swebench.harness`）。Agent 阶段在 Windows 可跑；判分请把 `predictions.jsonl` 拷到 Linux 后：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path artifacts/swebench_lite_dev_live/predictions.jsonl \
  --max_workers 1 \
  --run_id fixloop-lite-dev
```

或在本机加 `--run-harness`（非 Windows 时由 Adapter 子进程调用）。
---

## 3. 链路

```text
Lite 实例 (HF 或 JSONL)
        │
        ▼
  checkout base_commit  →  work-root/<instance_id>
        │
        ▼
  instance_to_issue()   →  Orchestrator.repair()
        │
        ▼
  export model_patch    →  predictions.jsonl + instances/*/model_patch.diff
        │
        ▼
  (可选) swebench.harness → resolved / 报告
        │
        ▼
  failure_class: env | agent | eval
```

---

## 4. 失败归因

| 类 | 含义 | 典型原因 |
|----|------|----------|
| `env` | 环境 | 无 datasets、clone/checkout 失败、未装 swebench、Docker 不可用 |
| `agent` | Agent | 无 patch、repair 异常、超时、dry-run |
| `eval` | 评测 | harness 跑完未 resolved、patch 不可应用 |
| `none` | 成功或待 harness | 有 patch 且 resolved / 或尚未判分 |

---

## 5. Manifest 字段

`manifest.json` 锁定：`instance_ids`、dataset 名、model/provider、预算（retries/timeout）、FixLoop commit、`adapter=swebench-adapter-v1`。

同系列报告**禁止**事后更换 Case 列表。

---

## 6. 输出布局

```text
artifacts/swebench_lite_dev/
  manifest.json
  predictions.jsonl
  adapter_report.json
  instances/<id>/result.json
  instances/<id>/model_patch.diff
```

---

## 7. 测试

```bash
pytest tests/test_swebench_adapter.py -v
```

---

## 8. 与自建 Eval 的关系

自建 `src/eval` Case 仍是日常回归主路径；本 Adapter 只解决「官方 Lite/Verified 口径」接入。面试口径：Lite 开发集用于证明链路可复现，不以刷分为目标。
