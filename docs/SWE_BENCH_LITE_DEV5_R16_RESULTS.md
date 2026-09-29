# SWE-bench Lite Dev5 R16 运行记录

## 运行口径

- 日期：2026-08-10
- 分支：`M5/D22/swebench-lite-dev5-r15`（含未提交 runtime / Verifier 改动）
- 数据集：`princeton-nlp/SWE-bench_Lite`，固定 Dev5
- Provider：`anthropic_compat`，实际模型 `deepseek-v4-pro`
- 每题 repair timeout：900 秒
- `max_retries=1`
- FixLoop Verifier：开启
- 官方 SWE-bench Harness：未启用
- 正式产物：`artifacts/swebench_lite_dev_live_r16_run/`
- 独立工作副本：`artifacts/swebench_repos_r16/`

Patcher 仍只接收公开问题与当前源码，不接收 Gold Patch、Gold Test Patch、
`FAIL_TO_PASS` 或 `PASS_TO_PASS` 等答案性信息。

## 结果总览

| Instance | 基线 | 补丁 | Patcher 终态 | Critic | FixLoop Verifier | 耗时 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `astropy__astropy-12907` | clean | 0 B | `needs_more_context` / `model_output_truncated` | 未运行 | 未运行 | 254.3 s |
| `django__django-11099` | clean | 670 B | `patch_produced` | 接受：`rules_first/ok` | 通过：22/22 | 220.4 s |
| `matplotlib__matplotlib-23964` | clean | 0 B | `needs_more_context` / `model_output_truncated` | 未运行 | 未运行 | 351.4 s |
| `pylint-dev__pylint-6506` | clean | 0 B | `needs_more_context` / `context_overflow` | 未运行 | 未运行 | 43.7 s |
| `sympy__sympy-20590` | clean | 0 B | `needs_more_context` / `final` | 未运行 | 未运行 | 644.3 s |

汇总：

- Baseline Clean Rate：5/5（100%）
- Non-empty Patch Rate：1/5（20%）
- Critic Accepted：1/1
- FixLoop Verifier Passed：1/1
- Provider HTTP 400：0
- Official Harness Resolved：未评测

## Case 明细

### Astropy

- Run ID：`bb97a976-c63e-4004-b330-0740ff551fcb`
- 6 个计费工具步骤，0 写入，95,627 tokens。
- 正确锁定 `astropy/modeling/separable.py`。
- 因重复读取进入 Converge；Trace 有 1 次 `post_lock_read_reserved`，但随后的
  同路径读取先被 StepGuard 判为 duplicate，保留读取未真正执行。
- 最后两次模型输出均达到 8192 tokens，只有 `thinking` block。新恢复逻辑安全
  丢弃两次截断内容，并以 `model_output_truncated` 终止；未再出现 HTTP 400。

### Django

- Run ID：`04670567-0e86-47c1-b804-899a54c85f04`
- 修改 `django/contrib/auth/validators.py`，两处正则从
  `^[\w.@+-]+$` 改为 `\A[\w.@+-]+\Z`。
- 导出补丁：
  `artifacts/swebench_lite_dev_live_r16_run/instances/django__django-11099/model_patch.diff`
- Critic：`accepted=true`，`reason=ok`，verdict ID
  `critic-821f8d2b8a2d`。
- Verifier：runtime probe 正常；`auth_tests.test_validators` 共 22 项，
  `22 passed / 0 failed`。
- Verifier 构建日志仍包含无法联网安装可选 `license-file` 的警告，但 editable
  Django 安装与目标测试均成功，未影响验证结论。

### Matplotlib

- Run ID：`b6b95f68-373d-4640-be69-8907d62e3b55`
- 6 个计费工具步骤，0 写入，127,512 tokens。
- 正确锁定 `lib/matplotlib/backends/backend_ps.py`。
- 与 Astropy 相同，`post_lock_read_reserved` 已产生，但同路径读取被更早的
  duplicate/convergence gate 拦截。
- 最后两次输出均为 8192-token `thinking` block，安全终止为
  `model_output_truncated`，无 HTTP 400。

### Pylint

- Run ID：`8431cc09-f52b-42a0-b003-30ca905f9d47`
- 0 工具步骤；第一轮上下文构建即失败：`actual=33089 limit=4000`。
- 与 R15 相同，仍是大上下文无法进入 Patcher 工具循环，新增收敛和截断恢复
  对该路径没有生效机会。

### SymPy

- Run ID：`6f087c87-446d-442d-8fa6-03efc8fb2836`
- 11 个计费工具步骤，0 成功写入，219,583 tokens。
- 已定位到 `Symbol` MRO / `__slots__` 机制，并扩锁
  `sympy/core/expr.py`；方向比 R15 更接近写补丁。
- 一次 8192-token 截断被安全恢复，后续模型继续运行，没有 HTTP 400。
- `post_lock_read_reserved` 产生后，读取仍被 Converge gate 拒绝；因此
  `apply_patch` 和后续 `patch_file` 均被 EditLock 以
  `unread_before_write:sympy/core/expr.py` 拒绝。
- 模型最后明确报告 `cannot_patch`，但外层仍归类为 `parse_fail`，说明终态映射
  仍需修正。

## R15 对比

| 指标 | R15 | R16 |
| --- | ---: | ---: |
| 非空补丁 | 1/5 | 1/5 |
| Critic 接受 | 1/1 | 1/1 |
| FixLoop Verifier 通过 | 0/1（环境失败） | 1/1（22/22） |
| Provider HTTP 400 | 3 case | 0 case |
| 明确截断终态 | 无 | Astropy、Matplotlib |

本轮确认已解决两项基础问题：Verifier 环境可用，截断恢复不再构造非法 Provider
消息。补丁率未提升的主要新阻塞是门控顺序：StepGuard 在 Quota reserve 之前拒绝
post-lock read，使“扩锁后必须读取”与“收敛后禁止读取”形成运行时矛盾。

## 排除的环境试跑

- `artifacts/swebench_lite_dev_live_r16/`：使用 `--skip-clone`，旧 `.agent`
  产物被 strict preflight 判为 `baseline_dirty`，未调用 Agent。
- `artifacts/swebench_lite_dev_live_r16_valid/`：独立 clone 由沙箱用户创建，实际
  运行用户触发 Git `dubious ownership`，未调用 Agent。
- 正式批次通过进程级 `safe.directory` 配置运行，未修改全局 Git 配置。

## 证据位置

- Adapter 报告：
  `artifacts/swebench_lite_dev_live_r16_run/adapter_report.json`
- Predictions：
  `artifacts/swebench_lite_dev_live_r16_run/predictions.jsonl`
- 每题 Trace：
  `artifacts/swebench_repos_r16/<instance>/.agent/runs/<run_id>/trace.jsonl`
- 每题 Repair State：
  `artifacts/swebench_repos_r16/<instance>/.agent/repairs/<run_id>/repair_state.json`
