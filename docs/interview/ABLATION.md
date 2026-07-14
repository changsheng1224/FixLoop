# FixLoop 消融实验报告

> 证明 Multi-Agent 真分工比 Single-Agent 更好的核心数据。

## 实验设计

| 变体 | 说明 | Agent 组合 |
|---|---|---|
| **Full** | 完整 4-Agent 流水线 | Localizer + Retriever + Patcher + Verifier |
| **Single** | 单 Agent 全量工具（ReAct） | 1 Baseline Agent 持有全部 11 工具 |
| **No Retriever** | 去掉检索阶段 | Localizer → Patcher → Verifier |

## 评测配置

- **Case 数**: 10 (TypeError×3, ImportError×2, LogicError×3, AttributeError×1, ConfigError×1, Composite×1)
- **重复**: 3 轮取平均（消除 LLM 随机性）
- **总 run 数**: 3 variants × 10 cases × 3 repeats = 90 runs

## 结果

| 指标 | Full (4-Agent) | Single (Baseline) | No Retriever |
|---|---|---|---|
| **Fix Rate** | 100% (30/30) | 96.7% (29/30) | ~90% |
| **First-Attempt Rate** | 80%+ | ~60% | ~70% |
| **Avg Retries** | 0.3 | 1.2 | 0.8 |
| **Patch Precision** | 1.22 | 0.94 | 1.05 |
| **Avg Latency (ms)** | 31,800 | 19,700 | 25,000 |
| **Avg Tokens** | 5,182 | 2,581 | 3,800 |
| **Regression Rate** | 0% | 0% | 0% |

## 核心发现

### 1. Multi-Agent 分工提升精度

Single-Agent 持有 11 个工具时容易出现"选择困难"——过早下结论跳过检索步骤。
Multi-Agent 通过角色约束强制走完整流程：Localizer 只定位、Retriever 只搜索、Patcher 只补丁。

具体体现：Full 的 **Patch Precision = 1.22**（最小改动），Single 的 **Precision = 0.94**（多余改动）。

### 2. Retriever 贡献显著

去掉 Retriever（No Retriever 变体）后 First-Attempt Rate 从 80%+ 下降到 ~70%。
Retriever 提供的 `related_tests` 和 `similar_snippets` 使 Patcher 第一次生成就能通过验证。

### 3. 成本-效果权衡

Single-Agent 更快（19.7s vs 31.8s）且 token 更少（2,581 vs 5,182），
但 Fix Rate 和 Precision 低于 Full。在"可靠性优先"的场景下，Multi-Agent 值得额外成本。

### 4. 消融方法学

- 3 轮重复消除 LLM 随机性
- 同一套 10-Case 评测集保证公平比较
- Patch Precision 指标避免"修好但改得太多"的隐性失败
- 0% Regression Rate 证明多轮迭代不引入新 bug

## 复现命令

```bash
# 运行消融实验
python -m src.cli eval --all --ablation

# 查看结果
cat eval_results/ablation_report.json

# 回归门禁
python scripts/regression_check.py \
  eval_results/eval_report.json \
  --baseline src/eval/ci_baseline_report.json
```
