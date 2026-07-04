# M7–M8 可改进与可额外实现功能探索

> 覆盖 `src/eval/` 评测体系、消融、CI 与 M8 交付物。Layer 2 流水线见 [`bonus_layer2_plan.md`](bonus_layer2_plan.md)。  
> 基线：`master` @ PR #83（10 Case verified，正式消融 60 runs：full + single × 3）。

---

## 1. 评测 Case 库 — `src/eval/cases/`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Case 011–020**：按错误类型矩阵新增 10 个 repo + `expected_patch.diff` + `min_lines.txt`。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 难度重标定**：依据 60 runs 将「Single 亦高 Fix Rate」的 Case 上调难度或标 `requires_retriever`。
- **[P2] [C:⭐ I:⭐⭐⭐] 负样本 Case**：缺测试或 ambiguous issue，期望 `exhausted`，防止 Fix Rate 虚高。
- **[P2] [C:⭐ I:⭐⭐⭐] metadata 扩展**：增 `requires_retriever`、`flaky`、`tags` 供 ablation 筛选。
- **[P3] [C:⭐⭐ I:⭐⭐] 多语言 Case 种子**：最小 Node repo + `language: javascript`。

---

## 2. Case 工具链 — `scripts/scaffold_eval_cases.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 脚手架完善**：交互生成目录骨架 + `verify-case` 一键「bug 红 → patch 绿」。
- **[P2] [C:⭐ I:⭐⭐⭐] Case 校验 CI**：扩展 `test_eval_cases.py`，每个 Case 断言 pre fail / post pass。
- **[P2] [C:⭐ I:⭐⭐] min_lines 自动生成**：从 `expected_patch.diff` 统计 `+` 行写入 `min_lines.txt`。

---

## 3. 补丁质量评分 — Runner 输出

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] patch_equivalence_score**：归一化对比 actual vs expected diff，输出 `full|partial|none`。
- **[P2] [C:⭐ I:⭐⭐⭐] 过度修改标记**：pytest 过但 `actual_lines >> minimal_lines` 时标 `overfit_patch`。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多解容忍**：`expected_patches/` 目录，任一 diff 匹配即算对。

---

## 4. 评测叙事 — README / 简历

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 结论重写**：Fix Rate 接近时强调耗时、token、patch_precision（full 1.22 vs single 0.94）。
- **[P2] [C:⭐ I:⭐⭐⭐] Case 级明细表**：README 折叠块列出每 Case 三变体 Fix/Retry/Token。
- **[P2] [C:⭐ I:⭐⭐⭐] no_retriever 正式 30 runs**：补全 90-run 消融，量化 Retriever 边际贡献。

---

## 5. EvalRunner — `src/eval/runner.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 并行跑 Case**：`run_all(workers=N)` + 进程池，注意 API 限流与 temp 隔离。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] --resume 断点续跑**：读 jsonl / report 跳过已完成 `(variant, case, rep)`。
- **[P2] [C:⭐ I:⭐⭐⭐] --keep-tmp**：单 Case 调试保留 temp repo 路径。
- **[P2] [C:⭐ I:⭐⭐⭐] regression 判定细化**：对比修复前后失败测试集合而非仅 exit code。
- **[P2] [C:⭐ I:⭐⭐] ablation --dry-run**：打印将运行次数与预估 token/费用。

---

## 6. 消融实验 — `src/eval/ablation.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 完整 90-run 报告**：full / single / no_retriever × 10 × 3 输出 JSON + Markdown。
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统计显著性**：bootstrap 或 McNemar，报告 fix_rate / duration 的 95% CI。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 变体扩展**：如 `no_localizer`、仅 direct patcher，独立小 PR。
- **[P2] [C:⭐ I:⭐⭐⭐] Flaky 检测**：同 `(variant, case)` 三次结果不一致标 `flaky=True`。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 多模型消融**：`--model` 交叉表，依赖 bootstrap 多 provider。

---

## 7. 指标与报告 — `src/eval/metrics.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 费用列**：token × 单价表 → `estimated_cost_usd` 与「$/fix」。
- **[P2] [C:⭐ I:⭐⭐⭐] JSON 解析成功率**：从 agent_errors / 空 suspects 聚合 `json_parse_fail_rate`。
- **[P2] [C:⭐ I:⭐⭐⭐] HTML 报告**：`format_html` 生成本地 dashboard。
- **[P2] [C:⭐ I:⭐⭐] CSV 导出**：`cases.csv` 供附录或 Excel。

---

## 8. Fake vs Real 双轨

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 文档分层**：README 明确 Fake=CI 形状、Real=论文数据，避免误解。
- **[P2] [C:⭐ I:⭐⭐⭐] Fake schema 对齐**：模拟 retry/token 字段，与真实 report 同结构。

---

## 9. Baseline 与 Token — `eval/baseline.py` / `token_usage.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 公平预算**：`--fair-budget` 统一 Multi 与 Single 总 step/token 上限。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Baseline 验证重试环**：Single-Agent 可选 patch→verify→feedback，或文档声明不对等。
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent token 表**：ablation summary 增 `by_agent`  breakdown。

---

## 10. Eval CLI — `src/cli.py eval` / `eval/__main__.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] repair 退出码与 --json**：与 Layer 2 CLI 一致，eval 脚本可解析 repair 结果。
- **[P2] [C:⭐ I:⭐⭐⭐] eval doctor**：检查 cases verified 数、Docker、API key、目录结构。
- **[P2] [C:⭐ I:⭐⭐] CLI 入口文档统一**：`src.cli eval` 与 `python -m src.eval.runner --ci` 行为对照表。

---

## 11. 评测 Trace 归档

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] run  artifact 目录**：`eval_results/runs/{ts}/{case}/` 存 repair_state、actual_patch、pytest log。
- **[P2] [C:⭐ I:⭐⭐⭐] JSONL 对齐 Layer 1 trace**：ablation_runs.jsonl 字段与 trace.jsonl 可互转。
- **[P2] [C:⭐ I:⭐⭐] Redaction**：归档前剥离 key 与绝对路径。

---

## 12. CI/CD — `.github/workflows/`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 分层 CI**：PR 跑 test；eval 用 weekly + manual；可选 eval-smoke 2 Case fake。
- **[P2] [C:⭐ I:⭐⭐⭐] PR 评论 bot**：upload artifact 后 `gh pr comment` 贴 regression 摘要。
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] Nightly real API smoke**：Secrets 存 key，1 Case × 1 变体，失败开 Issue 不 block master。
- **[P2] [C:⭐ I:⭐⭐] push 触发 eval 开关**：commit message `[skip eval]` 可跳过。

---

## 13. 回归基线 — `regression_check.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 双基线 profile**：`ci`（fake）与 `real`（gitignore 人工 bump）分开对比。
- **[P2] [C:⭐ I:⭐⭐⭐] bump 基线脚本**：`scripts/bump_eval_baseline.sh` + PR 审核更新 `ci_baseline_report.json`。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Refactor 后 eval 门禁**：大 refactor PR 必跑 `pytest` + `eval --ci` fake 全 Case。

---

## 14. Demo 与对外展示

- **[P1] [C:⭐ I:⭐⭐⭐⭐] demo_2 真实 API 说明**：文档标注 key 需求或提供 `--fake` 模拟两轮 retry。
- **[P2] [C:⭐ I:⭐⭐⭐] demo_lib 统一**：颜色、计时、exit code 检查共用。
- **[P2] [C:⭐ I:⭐⭐⭐] demo_4_eval.sh**：fake 跑 3 Case 打印 Markdown 表，面试现场无 API。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] asciinema / GIF**：90 秒 repair 演示嵌 README。
- **[P3] [C:⭐ I:⭐⭐] Release 附件**：样例 eval 报告 zip 随 GitHub Release，不进 git。

---

## 15. 文档与交付 — M8 产物

- **[P1] [C:⭐ I:⭐⭐⭐⭐] FINAL_STATS 更新**：484 tests、PR #83 模块列表、行数口径。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] SAMPLE_report.md**：脱敏样例报告进 repo，补 gitignore 外的可引用数据。
- **[P2] [C:⭐ I:⭐⭐⭐] ADR-011 Fix Rate vs 效率**：记录 Case 偏易时的叙事策略。
- **[P2] [C:⭐ I:⭐⭐⭐] 简历 Bullet 量化句**：消融 60 runs、patch_precision、token 对比写入 `RESUME_BULLETS.md`。
- **[P2] [C:⭐ I:⭐⭐⭐] v1.0.0 tag + Release Notes**：M8 范围外但投递前建议。
- **[P3] [C:⭐ I:⭐⭐] GitHub About / Topics**：品牌展示，非功能必需。

---

## 16. 生态扩展

- **[P2] [C:⭐⭐ I:⭐⭐⭐] Eval 关联 run_id**：一次 eval repair 链接 `.agent/runs/` 可 replay。
- **[P3] [C:⭐⭐⭐ I:⭐⭐⭐] SWE-bench Lite 适配**：Issue 转换 + 沙箱只读 repo，独立里程碑。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] HF Dataset 导出**：cases 元数据公开，不含 solution 泄露。

---

## 17. 测试补强

- **[P1] [C:⭐ I:⭐⭐⭐] patch_equivalence 单测**：相同/不同 diff 打分边界。
- **[P2] [C:⭐ I:⭐⭐⭐] Fake vs Real schema 契约测**：report 字段齐全性。
- **[P2] [C:⭐ I:⭐⭐] no_retriever fake e2e**：三变体各 1 Case 冒烟。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Docker eval job**：CI optional 跑 case_001 sandbox。

---
