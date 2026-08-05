# SWE-bench Lite Case 代码修改约束

> 用 Lite 开发集（先 5 题，后扩 10–30）做失败驱动迭代时的改码红线。  
> 目标：根据失败原因优化 Agent / Adapter / Harness，同时避免针对单题过拟合。  
> 相关：`docs/SWE_BENCH_ADAPTER.md`、开发集产物 `artifacts/swebench_lite_dev_live/`。  
> 5 题问题与证据：`docs/SWE_BENCH_LITE_DEV5_FAILURES.md`（R1/R2）；R3：`docs/SWE_BENCH_LITE_DEV5_R3_FAILURES.md`；R4：`docs/SWE_BENCH_LITE_DEV5_R4_FAILURES.md`；R5：`docs/SWE_BENCH_LITE_DEV5_R5_FAILURES.md`；R6：`docs/SWE_BENCH_LITE_DEV5_R6_FAILURES.md`。

---

## 1. 总原则

1. **改「问题类」，不改「题面」**：每个 PR / 改动必须对应一个失败类（如 CRLF 导出、路径清洗、Gateway 角色、step 预算），标题与说明写类名，不写 `django-11099`。
2. **证据先行**：先有 failure card（症状 → 证据路径 → 根因假设 → 通用修复），再动代码。
3. **双指标解读**：管道指标（`nonempty_patch` / `patch_apply_ok` / `harness_ok`）与能力指标（`resolved`）分开；apply 失败不算「模型不会修」。
4. **调试集 ≠ 刷分集**：5 题只用于发现类问题；调参不以单题 Resolved 为唯一成功标准。

---

## 2. 禁止（过拟合红线）

| 禁止 | 例子 |
|------|------|
| instance / repo 特判 | `if instance_id == "django__django-11099"` |
| golden patch / 标准答案写入 | 把官方 patch 或固定 diff 塞进 prompt/测试期望 |
| 题面关键词硬编码 | 专门匹配 `ASCIIUsernameValidator`、某 issue 标题 |
| 为单题无限加预算 | 只给某一题 `max_steps=999` |
| 伪造路径/文件名提示 | prompt 里写死该题真实修复文件 |
| 用 held-out 题调参 | 扩集后拿同一批反复改到分涨 |

---

## 3. 允许（通用机制）

| 允许 | 说明 |
|------|------|
| 跨题复现的管道修复 | patch LF 规范化、二进制跳过、UTF-8 安全导出 |
| 跨题复现的工具/权限 | Gateway 角色与 `suggested_tools` 对齐；非法路径拒绝 |
| Issue 栈路径清洗 | 绝对路径 → workspace 相对路径；剥离他人机器前缀 |
| Schema / JSON 校验 + 有限 retry | 任何题都可能坏输出 |
| SWE 向通用模板 | 症状/期望/相关测试暗示，不含具体文件名 |
| 同文件「同类 pattern」扫描 | 机制级（如同文件多处相同 regex 锚点），不写符号名 |
| 失败细分类 | `patch_apply` / `tests_failed` / `env` / `parse_fail` |

判定口诀：**拿掉 instance_id 后，这条规则对任意 Lite 题是否仍成立？** 否 → 不许合。

---

## 4. 迭代流程约束

每轮固定：

```text
归因（类）→ 选 1 个类 → 通用实现 + 单测 → 相关测 → 5 题复跑（同 Manifest）→ 记录前后指标
```

1. **一轮一类**：禁止一个 PR 混修 CRLF + Gateway + Prompt 大改。
2. **先管道后能力**：E1 导出/apply → E2/E3 工具面 → E4/E5 定位与预算 → E7 完整性。
3. **止损**：单类约 45–60 分钟无通用方案 → 记 Backlog，换类；环境问题单独记，不阻塞 Agent 类。
4. **回归**：改 Agent Runtime 跑相关单测；改 adapter 跑 `tests/test_swebench_*.py`；宣称评测改善必须有复跑报告。
5. **对照**：目标类改善时，非目标题不得无说明地明显变差。

---

## 5. Failure Card 必填字段（改前）

```yaml
class_id: E1_crlf   # 稳定 ID，非题号
symptom: "..."
evidence:
  - path: artifacts/.../run_instance.log
    quote: "different line endings"
affected_count: 1   # 本轮 5 题中命中数；扩集后更新
root_cause_hypothesis: "Windows diff 含 CR"
fix_level: adapter_export | gateway | prompt | budget | ...
generic_rule: "所有 model_patch 导出统一 LF"
anti_overfit_check: "不依赖 django / validators.py"
acceptance:
  - unit: "含 \\r\\n 的 diff 规范化后无 CR"
  - smoke: "django-smoke patch_apply 不再因 line endings 失败"
out_of_scope: "不保证该题 Resolved"
```

无 card 不写业务代码。

---

## 6. PR / 提交约束

- 标题：`fix(swebench): normalize patch LF on export`（类）  
  而非：`fix: django-11099 harness apply`
- Body 必须含：`class_id`、通用规则一句话、反过拟合自检、验证命令。
- 测试：优先**合成夹具**（假 diff、假绝对路径、假 role），不要依赖真实 Lite 仓库内容当唯一断言。

---

## 7. 与 5 题证据的映射（只指导优先级，不写进代码）

| class | 5 题证据用途 | 实现时只能抽象成 |
|-------|--------------|------------------|
| E1 CRLF | django apply 失败 | 一切 patch 导出 LF |
| E2 路径 | matplotlib/pylint 绝对路径 | Issue/栈路径清洗 |
| E3/Gateway | role_not_allowed | 角色-工具矩阵 |
| E5 预算 | step_limit + 拒绝仍计数 | 预算策略通用调整 |
| E6 环境 | WSL/代理 | 文档与后端，少改 Agent |
| E7 不完整 | ASCII vs Unicode | 同类 pattern 机制 |

详细证据见会话归因与 `artifacts/swebench_lite_dev_live/`（adapter / harness / `.agent/runs`）。

---

## 8. 扩集门禁

- 5 题飞轮 **管道指标稳定**（尤其 `patch_apply_ok`）后再开 10–30。
- 扩集后：**调试集可观察，调参集与报告集分离**；报告集只评不调。
- 仍禁止为抬某一扩集题写特例。

---

## 9. 一句话纪律

> Lite case 只提供**失败形态的样本**；代码只实现**可迁移的机制**。若修复离开这 5 个 id 就失效，则视为过拟合，不予合并。
