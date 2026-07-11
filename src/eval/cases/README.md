# M7 评测 Case 库

> M7D1 任务 1 产出：10 个 Case 目录骨架 + 覆盖矩阵。  
> `repo/` 与标注文件在 M7D1 任务 4–9 / M7D2 任务 1 中填充。

## 目录结构

每个 Case 目录：

```
case_XXX/
├── issue.txt              # 错误描述 / 堆栈（模拟 GitHub Issue 或 CI 日志）
├── expected_patch.diff    # 人工标注的最小正确修复（unified diff）
├── min_lines.txt          # 最小必要修改行数（整数，供 patch_precision 指标）
├── metadata.yaml          # 元数据（类型、难度、预估耗时等）
└── repo/                  # 含 bug 的微型 Python 项目（可独立 pytest）
```

## 覆盖矩阵（5 类错误 × 2–3 难度 → 10 Case）

| Case ID | 错误类型 | 难度 | 一句话描述 | M7 计划日 | 状态 |
|---------|----------|------|------------|-----------|------|
| `case_001` | TypeError | 简单 | 二元运算参数类型未转换（`str` + `int`） | D1 | **verified** |
| `case_002` | TypeError | 中等 | 返回值类型与调用方期望不符 | D1 | **verified** |
| `case_003` | TypeError | 中等 | 未判空导致对 `None` 运算或拼接 | D1 | **verified** |
| `case_004` | ImportError | 中等 | 模块路径错误（`utils.helper` → `helpers`） | D1 | **verified** |
| `case_005` | ImportError | 中等 | 错误 import 符号名（`hello` → `greet`） | D1 | **verified** |
| `case_006` | logic_error | 中等 | off-by-one 闭区间 `range(start, end)` | D1 | **verified** |
| `case_007` | AttributeError | 中等 | 对 `None` profile 取 `display_name` | D1 | **verified** |
| `case_008` | logic_error | 困难 | 三跳调用链中 `normalize_score` 多除 100 | D1 | **verified** |
| `case_009` | config_error | 困难 | `pyproject.toml` 缺少 `[tool.eval]` 配置段 | D2 | **verified** |
| `case_010` | composite | 困难 | 错误 import + `run_task` 类型运算（两文件） | D2 | **verified** |

### 按错误类型聚合

| 错误类型 | Case | 难度分布 |
|----------|------|----------|
| TypeError | 001, 002, 003 | 简单 ×1，中等 ×2 |
| ImportError | 004, 005 | 中等 ×2 |
| logic_error | 006, 008 | 中等 ×1，困难 ×1 |
| AttributeError | 007 | 中等 ×1 |
| config_error | 009 | 困难 ×1 |
| composite | 010 | 困难 ×1 |

### 与现有 demo 的关系

| Demo | 可借鉴点 | 评测 Case |
|------|----------|-----------|
| `demo/calculator` | TypeError 类型转换 | → `case_001` 思路类似，但独立 repo |
| `demo/importer` | ImportError 路径 | → `case_004` / `case_005` |
| `demo/logic_bug` | off-by-one | → `case_006` |

评测 Case **不直接 symlink demo**，以保证 `expected_patch.diff` / `min_lines.txt` 标注独立、Runner 可复制 `repo/` 到临时目录。

## 标注说明

### `issue.txt`

- 格式：标题行 + 堆栈或 CI 摘录（与 `python -m src.cli repair --issue` 输入一致）。
- 应含足够信息供 Localizer 定位文件与行号，但不必包含修复提示。

### `expected_patch.diff`

- 标准 unified diff，相对于 `repo/` 根目录。
- 仅包含**最小必要修改**；评测时用于人工核对或后续自动 diff 对比。

### `min_lines.txt`

- 单行整数：期望补丁修改的有效代码行数（不含空行与纯注释行）。
- 用于 M7 `patch_precision = min_lines / max(actual_lines, 1)`。

### `metadata.yaml`

| 字段 | 说明 |
|------|------|
| `case_id` | 与目录名一致 |
| `language` | 固定 `python`（M7 范围） |
| `issue_type` | `type_error` / `import_error` / `logic_error` / `attribute_error` / `config_error` / `composite` |
| `expected_skill` | 期望命中的 Skill 名称（`src/skills/*.yaml` 的 `name`）；用于 Skill 召回率 eval |
| `difficulty` | `easy` / `medium` / `hard` |
| `status` | `scaffolded` → `ready`（可 pytest 复现）→ `verified`（补丁已验证） |
| `estimated_duration_s` | 单次 repair 预估耗时（供评测排期） |
| `description` | 一句话说明 |
| `tags` | 可选标签，便于消融分组 |

## 使用方式（M7D2 Runner 就绪后）

```bash
# 自动化（Fake，无需 API）
python -m src.cli eval --fake --all --verbose
python -m src.cli eval --fake --case case_001 --output eval_results/report.json

# Skill 召回率（离线，无需 API）
python -m src.cli eval skills --all --verbose
python -m src.cli eval skills --case case_001 --output eval_results/skill_eval_report.json

# 或
python -m src.eval.runner --fake --all
```

## 验收（M7D1）

- [x] `src/eval/cases/` 目录存在
- [x] `case_001` … `case_010` 共 10 个子目录
- [x] 每目录含标准文件与 `repo/`
- [x] **case_001–010**：bug 可 `pytest` 复现，`expected_patch.diff` 可修绿
- [x] `EvalRunner` + `tests/test_eval_runner.py`（`--fake` 模式）
