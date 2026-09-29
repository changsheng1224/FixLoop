# SWE-bench Lite Dev5 R15 运行记录

## 运行口径

- 日期：2026-08-10
- 分支：`M5/D22/swebench-lite-dev5-r15`
- 数据集：`princeton-nlp/SWE-bench_Lite`，固定 Dev5
- Provider：`anthropic_compat`（模型由本地 DeepSeek 配置解析）
- 每题 repair timeout：900 秒
- `max_retries=1`
- FixLoop Verifier：开启
- 官方 SWE-bench Harness：未启用
- 严格隔离：Patcher 只接收公开 `problem_statement`，不接收 instance ID、
  base commit、Gold Patch、Gold Test Patch、`FAIL_TO_PASS` 或 `PASS_TO_PASS`

正式产物位于 `artifacts/swebench_lite_dev_live_r15/`。本记录中的
`verified=false` 只表示 FixLoop Verifier 未通过，不能解释为官方 Harness 未解决。

## 结果总览

| Instance | 基线 | 补丁 | Patcher 终态 | Critic | Verifier | 耗时 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `astropy__astropy-12907` | clean | 0 B | `needs_more_context` | 未运行 | 未运行 | 89.7 s |
| `django__django-11099` | clean | 668 B | `patch_produced` | 接受：`rules_first/ok` | 未通过：`env`，0 tests | 373.8 s |
| `matplotlib__matplotlib-23964` | clean | 0 B | `needs_more_context` | 未运行 | 未运行 | 68.9 s |
| `pylint-dev__pylint-6506` | clean | 0 B | `needs_more_context` | 未运行 | 未运行 | 19.6 s |
| `sympy__sympy-20590` | clean | 0 B | `needs_more_context` | 未运行 | 未运行 | 243.8 s |

汇总：

- Baseline Clean Rate：5/5（100%）
- Non-empty Patch Rate：1/5（20%）
- Critic Accepted：1/1 个进入 Critic 的补丁
- FixLoop Verifier Passed：0/1 个进入 Verifier 的补丁
- Official Harness Resolved：未评测

## 补丁生成

### Django

Run ID：`df334d34-77d1-4f02-a08b-a4b4541724db`

生成补丁修改 `django/contrib/auth/validators.py` 中两处 username validator，
将正则结尾锚点从 `$` 改为 `\Z`。导出补丁：

`artifacts/swebench_lite_dev_live_r15/instances/django__django-11099/model_patch.diff`

Critic 结果：

- mode：`rules_first`
- accepted：`true`
- reason：`ok`
- verdict ID：`critic-f049f2149232`

Verifier 结果：

- `verify_test_patch_applied=true`
- `all_passed=false`
- `total_tests=0`
- bucket：`env`
- 失败原因：Django runtests 未执行；容器输出 `/bin/sh: 1: /entrypoint.sh: not found`
- 结论：验证环境失败，不能据此判断补丁行为正确或错误

证据：

- `artifacts/swebench_repos/django__django-11099/.agent/repairs/df334d34-77d1-4f02-a08b-a4b4541724db/repair_state.json`
- `artifacts/swebench_repos/django__django-11099/.agent/runs/df334d34-77d1-4f02-a08b-a4b4541724db/trace.jsonl`

Verifier 环境修复后复验（2026-08-10）：

- 根因：`/entrypoint.sh` 实际存在，但使用镜像中不存在的 `/bin/bash`；Windows
  构建上下文还会带入 CRLF，使 `/bin/sh` 无法正确解析脚本。
- 修复：入口改用 `/bin/sh`，镜像构建时归一化 LF，并在 sandbox 创建阶段实际执行
  entrypoint/Python runtime probe。
- 复验范围：在独立临时 clone 中应用上述 Candidate Patch，只运行 FixLoop Verifier
  的 `auth_tests.test_validators`；未启用官方 Harness，也未应用官方 test patch。
- 结果：`all_passed=true`，`total_tests=22`，`passed=22`，`failed=0`，
  `runtime_probe=ok`。
- 复验同时修复了退出码解析问题：原逻辑会用 `exit_code or 1` 将成功码 `0`
  错写为 `1`，从而把 Django 的 `OK` 误报成失败。
- 结论：FixLoop Verifier 环境及结果解析已恢复；该结果不等价于官方 Harness 的
  instance resolved 判定。

### 其余四题

Astropy、Matplotlib、Pylint 和 SymPy 均未产生 Candidate Patch，因此 Critic
和 Verifier 按门禁未运行。这是“补丁生成失败”，不是“Verifier 判定补丁失败”。

- Astropy：12 tool steps；末次归因为 Provider `max_tokens` 后 HTTP 400，另有一次
  `run_shell` 权限拒绝。Run ID：`6e9dd20e-2907-492b-ada8-7d31e5807745`。
- Matplotlib：12 tool steps；末次归因为 Provider `max_tokens` 后 HTTP 400。
  Run ID：`1b030200-ef32-4cf2-be8a-7c4e274166ab`。
- Pylint：模型响应上下文 `actual=33089` 超过解析限制 `4000`，未进入工具循环，
  `patcher_parse_failed=true`。Run ID：`7f9d4614-2d99-4666-9f7f-00229ed8b142`。
- SymPy：15 tool steps；末次归因为 Provider `max_tokens` 后 HTTP 400。
  Run ID：`5ced98e2-0967-43ea-a102-a78da8a9d964`。

## 本轮隔离修复

正式运行前发现旧链路会把 Gold Test Patch 用于定位，并让 Prompt 中的
`RepairPlan.suspect_files` 与 Executor 的 Effective EditLock 冲突。本轮已修复：

1. SWE-bench repair issue 默认只包含公开 `problem_statement`。
2. `verify_test_patch` 不再传给 Patcher-primary 定位，只供 Verifier overlay 和测试选择。
3. Patcher Prompt 的允许文件和磁盘预读均读取 Effective EditLock。
4. 运行中检查确认 Patcher 输入不含 instance ID、base commit、`FAIL_TO_PASS`、
   `test_patch` 或 `test_patch覆盖`。

排除的非正式批次：

- `artifacts/swebench_lite_dev_live_r15_invalid_fixture_run/`：误用了单测 fixture 的
  占位 base commit，5/5 在 checkout 阶段失败。
- `artifacts/swebench_lite_dev_live_r15_gold_leakage_aborted/`：发现 Gold Test Patch
  进入 Patcher 定位后立即终止，不计入结果。

## 回归测试

- 首轮相关门禁：107 passed
- 隔离与 EditLock 修复后：66 passed
- Verifier 环境及退出码修复后：58 passed
- pytest 均只有 `.pytest_cache` 创建失败警告；未运行全量测试。
- lint 未运行：当前宿主 Python 环境未安装 `ruff`。

## 后续问题

1. 修复 Provider `max_tokens` 后 HTTP 400 的续写/终态处理，避免三题以
   `needs_more_context` 空补丁结束。
2. 修复 Pylint 的 33k 响应被 4k 解析上限拒绝问题。
3. Adapter 报告应直接记录 Critic verdict、Verifier bucket 和是否执行，减少离线
   拼接 `repair_state.json` 的成本。
