# list_files glob / depth — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §6.1 — list_files glob / depth [P2]
- **Layer:** L1
- **Primary modules:** `agent_runtime/file_listing.py`, `agent_runtime/tools.py`
- **Acceptance:** `pytest tests/test_file_listing.py tests/test_tools.py -v`
- **Branch:** `V1.1-Bonus2-Agent-Tool`

## 目标

`list_files` 支持 `glob` 过滤与 `depth` 限制递归；默认 `depth=1` 保持原有一层列举行为。

## 参数

| 字段 | 默认 | 说明 |
|------|------|------|
| `path` | `.` | 起始目录 |
| `glob` | `""` | fnmatch，如 `*.py` |
| `depth` | `1` | 1=直接子项；2–10 递归；0=不限层数 |
| `max_results` | `200` | 1–500，超出附截断提示 |

## 语义

- **depth=1**：输出 `[F]`/`[D]` + basename；glob 匹配 basename
- **depth≥2 或 0**：仅输出匹配的文件路径 `[F] rel/path`；目录仅作遍历
- 跳过 `IGNORED_PATH_NAMES` 与点号隐藏项

## 不在范围

- 内容搜索（`grep` §6.2）、L2 registry 改动
