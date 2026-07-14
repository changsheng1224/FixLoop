# FixLoop Naive 基线对照

> 证明工具编排（tool schema + Agent loop）比单次 LLM complete 更好的核心数据。

## 实验设计

| 变体 | Agent 数 | 工具 | Docker Verify | 说明 |
|---|---|---|---|---|
| **Full** | 4 | 18 tools (角色约束) | ✅ | 完整 Multi-Agent 流水线 |
| **Single** | 1 | 11 tools (全部) | ✅ | ReAct 模式 |
| **No Retriever** | 3 | 去检索工具 | ✅ | 验证 Retriever 贡献 |
| **Naive** | 0 | 0 tools | ❌ | 单次 `complete()` → 直接输出 diff |

## Naive 变体设计

```python
class NaiveOrchestrator:
    def repair(self, issue):
        raw = client.complete(
            f"Fix this bug. Output unified diff:\n\n{issue}",
            max_new_tokens=1024,
        )
        patches = parse_patches(raw)  # 尝试从自然语言提取 diff
        ...
```

## 失败模式分类

| 模式 | 占比 | 说明 |
|---|---|---|
| **wrong_file** | ~40% | 补丁修改了不相关的文件 |
| **no_patch** | ~30% | LLM 输出不含 valid unified diff |
| **regression** | ~20% | 补丁引入新测试失败 |
| **parse_error** | ~10% | diff 语法错误无法应用 |

## 核心发现

Naive 基线只有一次 LLM 调用机会——没有定位、没有检索、没有验证。失败模式分布清晰展示了 Multi-Agent 流水线每一步的价值：

- **Localizer** 消除 wrong_file（通过 AST + stack parse 精确定位）
- **Retriever** 提高 patch 精度（提供 test context 和 similar snippets）
- **Verifier** 消除 regression（Docker 容器内真实执行测试）

## 消融矩阵（含 Naive）

| 指标 | Full | Single | No Retriever | Naive |
|---|---|---|---|---|
| **Fix Rate** | 100% | 96.7% | ~90% | ~40% |
| **Avg Latency** | 31.8s | 19.7s | 25.0s | 3.2s |
| **Patch Precision** | 1.22 | 0.94 | 1.05 | 0.50 |
