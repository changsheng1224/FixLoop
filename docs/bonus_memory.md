# 记忆系统可改进与可额外实现功能探索

> 覆盖 Working Memory、Episodic Memory、Durable Memory、Semantic Memory 四层。

---

## 1. 工作记忆 — Working Memory

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] task_summary 模型生成**：当前 `set_task_summary` 直接截断用户输入前 300 字，应改为调用模型生成一句话摘要。有了 light_client 后可在 AgentLoop 中非阻塞完成
- **[P1] [C:⭐ I:⭐⭐⭐] recent_files 带访问时间**：每个文件加 `last_accessed_at` 字段，`/memory` 命令按时间排序展示
- **[P1] [C:⭐ I:⭐⭐⭐] 文件摘要自动过期**：`file_summaries` 增加 TTL（如 30 分钟），超时自动清理，防止多次 ask 后堆积
- **[P2] [C:⭐ I:⭐⭐⭐] recent_files 标注操作类型**：`[R] config.py` vs `[W] utils.py`，Agent 能区分"读过"和"改过"
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 文件变更检测联动**：`git status --short` 的结果与 `recent_files` 对比，标注"Agent 改的"vs"外部改的"
- **[P3] [C:⭐⭐ I:⭐⭐] 文件重要性加权**：被反复读取的文件排在前面，被 `write_file`/`patch_file` 修改的文件权重更高

---

## 2. 事件记忆 — Episodic Memory

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 检索结果带匹配分数**：当前 `retrieval_candidates` 只返回笔记列表，加上相似度分值后 Agent 可对低分结果表达不确定
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 自动标签生成**：当前 tags 由调用方手动指定，应从 note text 中用 TF-IDF 或关键词提取自动生成
- **[P2] [C:⭐⭐ I:⭐⭐⭐] episodic → durable 自动晋升**：kind="decision" 且多次被检索的笔记自动 promote 到 durable
- **[P2] [C:⭐ I:⭐⭐⭐] 时间范围检索**：`retrieval_candidates` 支持 `since="5m"` / `since="1h"` 过滤
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 笔记关联图**：记录 note 之间的因果关系（"读完 a.py → 发现 bug → patch a.py"），可视化推理链
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 笔记摘要合并**：多条相似笔记（cosine > 0.8）自动合并为一条，减少冗余

---

## 3. 持久记忆 — Durable Memory

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 检索返回 topic 标注**：当前 `retrieval(query)` 只返回纯文本，不标注来源 topic。加上后 Agent 说"根据你的偏好设置"而非模糊引用
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 条目带时间戳 + 自动归档**：每条 entry 加 `created_at`，超 N 天未被检索的条目自动移到 `topics/archive/`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 冲突检测与合并**：同 topic 下相似条目（cosine > 0.8）提示用户合并或自动合并
- **[P2] [C:⭐ I:⭐⭐⭐] `/memory search` REPL 命令**：搜索 durable memory 并高亮匹配
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] Git 版本控制**：每次 promote 后自动 `git add .agent/memory/ && git commit -m "memory: update"`，变更可追溯
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 导入导出**：`/memory export` 导出所有 topic 为 JSON，跨机器迁移
- **[P3] [C:⭐⭐ I:⭐⭐] 记忆可视化**：`/memory tree` 展示四层记忆的树状结构

---

## 4. 语义记忆 — Semantic Memory

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 模型预下载 + 离线可用**：支持 HF 镜像或离线缓存，当前 GFW 下首次加载会超时失败
- **[P1] [C:⭐ I:⭐⭐⭐] 模型加载状态暴露**：`/session` 显示当前 semantic model 是否可用
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 多语言模型切换**：`all-MiniLM-L6-v2` 对中文一般，换用 `paraphrase-multilingual-MiniLM-L12-v2`（118MB，50+ 语言）
- **[P2] [C:⭐ I:⭐⭐⭐] embedding 缓存**：相同文本不重复编码，缓存到 `~/.agent/embedding_cache/`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 可配置相似度阈值**：`search(query, top_k, min_similarity=0.3)` 中的 0.3 可通过 CLI 调整
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 向量索引加速**：笔记超 100 条后改用 FAISS/annoy 做 ANN 搜索，替代全量 cosine
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 增量索引**：新增笔记不重算全部相似度，只算新增笔记与已有笔记的相似度

---

## 5. 记忆系统整体增强

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 跨 ask 记忆共享**：同一 session 的多次 `ask()` 之间记忆不互通（每次 `ask()` 创建新 AgentLoop 但 Agent 不变），应确保 Working Memory 跨轮累积
- **[P1] [C:⭐ I:⭐⭐⭐] 记忆使用统计**：`/memory stats` 显示各层条目数、命中率、总 token 占用
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 记忆优先级仲裁**：当 memory section 超出 800 token 预算时，按重要性排序取舍（task_summary > recent_files > file_summaries）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 记忆与 checkpoint 联动**：create_checkpoint 时自动将当前 Working Memory 的快照存入 checkpoint
- **[P3] [C:⭐⭐⭐ I:⭐⭐⭐] 上下文感知记忆**：Agent 根据当前任务类型（debug / feature / refactor）自动调整记忆检索策略
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 记忆图谱可视化**：用 NetworkX 构建文件→笔记→决策的关系图，`/memory graph` 渲染

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
| **P1** | 8 项 | task_summary 模型生成 `C:⭐⭐ I:⭐⭐⭐⭐`、search 带分数 `C:⭐ I:⭐⭐⭐⭐`、跨 ask 共享 `C:⭐⭐ I:⭐⭐⭐⭐` |
| **P2** | 14 项 | 自动标签 `C:⭐⭐ I:⭐⭐⭐`、Git 版本控制 `C:⭐⭐⭐ I:⭐⭐⭐`、多语言模型 `C:⭐⭐⭐ I:⭐⭐⭐` |
| **P3** | 7 项 | FAISS 加速 `C:⭐⭐⭐⭐ I:⭐⭐`、记忆图谱 `C:⭐⭐⭐⭐ I:⭐⭐` |

**🏆 Top 5 最高投入产出比**：

| 排名 | 条目 | C | I | 层级 |
|:--:|------|:--:|:--:|------|
| 1 | 检索结果带匹配分数 | ⭐ | ⭐⭐⭐⭐ | Episodic |
| 2 | retrieval 返回 topic 标注 | ⭐ | ⭐⭐⭐⭐ | Durable |
| 3 | 文件摘要自动过期 | ⭐ | ⭐⭐⭐ | Working |
| 4 | 模型加载状态暴露 | ⭐ | ⭐⭐⭐ | Semantic |
| 5 | 记忆使用统计 | ⭐ | ⭐⭐⭐ | 整体 |

**总计 29 项**。
