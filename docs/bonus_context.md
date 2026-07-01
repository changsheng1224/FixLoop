# 上下文工程可改进与可额外实现功能探索

> 覆盖 Token 预算、Prompt 组装、历史压缩、对话摘要、Prompt Cache 等模块。

---

## 1. Token 预算 — TokenBudget

- **[P1] [C:⭐ I:⭐⭐⭐] 多模型 tokenizer 自动切换**：当前固定 `cl100k_base`，应根据 config.provider 选择对应 tokenizer（DeepSeek → cl100k，Anthropic → claude tokenizer）
- **[P1] [C:⭐ I:⭐⭐⭐] 实际 token 消耗追踪**：每次 `build()` 后在 trace 中记录各 section 的 `rendered_tokens` vs `budget_tokens`，对比预算与实际
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 动态预算分配**：简单任务（一轮 final）自动收紧预算；复杂任务（多轮 tool）自动放宽，避免"一刀切 6000"


---

## 2. Prompt 组装 — ContextManager

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] memory/relevant section 内容优先级排序**：当前 `_get_memory` 无脑输出 task_summary + recent_files + file_summaries，超出 800 token 预算时直接裁剪尾部。应改为按重要性排序（task > modified files > read files > old summaries）
- **[P1] [C:⭐ I:⭐⭐⭐] build() 性能优化**：当前每轮循环都 `tiktoken.encode()` 多次相同文本（prefix 不变部分），应缓存 token count
- **[P2] [C:⭐ I:⭐⭐⭐] build() metadata 暴露**：当前返回的 metadata 只被 AgentLoop 丢弃，应在 `/session` 或 trace 中展示


---

## 3. 历史压缩 — History Compression

- **[P1] [C:⭐⭐ I:⭐⭐⭐] 工具结果智能截断**：当前 `tool` 结果统一截断到 500 字符，应改为按工具类型差异化——`read_file` 保留更多，`list_files` 保留更少
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 重要信息标记保留**：工具结果中的 Error 行、文件路径、行号信息优先保留，即使超预算
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量压缩**：不是每轮从零压缩所有旧历史，而是在上一轮的压缩结果上追加新条目

---

## 4. 对话摘要 — LLM Summarization

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 摘要质量评估**：记录每次摘要的压缩比和关键实体保留率。质量差时自动调高 `trigger_tokens` 或换模型
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 摘要触发条件优化**：当前只看 token 数超 2600，应同时考虑轮数（超过 10 轮即使 token 不超也触发）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量摘要**：在已有摘要基础上追加新内容（"Earlier: 读取了 config.py。Update: 又读取了 tools.py，修复了 TypeError"），而非每次从头生成
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 摘要缓存**：同一段旧历史的摘要结果缓存，避免重复调模型

---

## 5. Prompt Cache — Cache Strategy

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 缓存命中率统计**：记录 `cache_hits / cache_misses`，`/session` 显示命中率
- **[P2] [C:⭐⭐ I:⭐⭐] 跨 session 缓存共享**：同一 workspace 的多个 session 共享 prefix 缓存

---

## 6. System Prompt — Prompt Engineering

- **[P1] [C:⭐ I:⭐⭐⭐⭐] rules 动态注入**：当前规则是硬编码的 7 条，应支持根据 `--approval` 和 `--dry-run` 动态增减（如 dry-run 时加一条"不要实际修改文件"）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] few-shot 示例从文件加载**：当前 `TOOL_EXAMPLES` 只有 3 个 tool 示例，应支持从 `.agent/examples.md` 加载项目特定的调用示例


---

## 评分维度说明

| 维度 | ⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
|------|------|------|------|------|------|
| **C (复杂度)** | 几分钟 | 几小时 | 1天 | 2-3天 | 1周+ |
| **I (重要性)** | 锦上添花 | 有更好 | 值得做 | 显著提升 | 核心竞争力 |

---

## 优先级汇总

| 优先级 | 数量 | 代表条目 |
|:--:|:--:|------|
| **P1** | 10 项 | 多模型 tokenizer `C:⭐ I:⭐⭐⭐`、摘要质量评估 `C:⭐ I:⭐⭐⭐⭐`、rules 动态注入 `C:⭐ I:⭐⭐⭐⭐` |
| **P2** | 14 项 | 自适应 section 顺序 `C:⭐⭐ I:⭐⭐⭐`、增量摘要 `C:⭐⭐ I:⭐⭐⭐`、few-shot 文件加载 `C:⭐⭐ I:⭐⭐⭐` |
| **P3** | 6 项 | Token 仪表盘 `C:⭐⭐ I:⭐⭐`、多轮摘要链 `C:⭐⭐ I:⭐⭐`、缓存预热 `C:⭐⭐⭐ I:⭐⭐` |

**🏆 Top 5 最高投入产出比**：

| 排名 | 条目 | C | I | 模块 |
|:--:|------|:--:|:--:|------|
| 1 | 摘要质量评估 + 自适应触发 | ⭐ | ⭐⭐⭐⭐ | Summarization |
| 2 | 缓存命中率统计 | ⭐ | ⭐⭐⭐⭐ | Prompt Cache |
| 3 | rules 动态注入 | ⭐ | ⭐⭐⭐⭐ | System Prompt |
| 4 | 压缩比统计 | ⭐ | ⭐⭐⭐ | History |
| 5 | 多模型 tokenizer 自动切换 | ⭐ | ⭐⭐⭐ | TokenBudget |

**总计 30 项**。
