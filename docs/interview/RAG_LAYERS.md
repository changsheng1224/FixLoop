# FixLoop RAG 三层架构

> 面试叙事：FixLoop 的检索增强生成（RAG）分三层，层层递进，每层服务不同粒度的上下文需求。

## 总览

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1  投影层 (Projection)                             │
│ ContextManager._get_knowledge                            │
│ episodic notes + durable facts → knowledge section       │
│ 触发：每次 prompt 构建                                   │
│ 检索：semantic + keyword 双路 → 去重 → top-k             │
│ 共享：derive_embed_query() 查询提取                      │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 2  流水线 (Pipeline)                               │
│ RepairPrecedentStore → similar_fixes                     │
│ 触发：Orchestrator.repair() 启动时                        │
│ 存储：dependency-facts topic (JSON lines in Markdown)    │
│ 检索：issue_type 精确匹配 + semantic cosine 过滤         │
│ trace: precedent_score 写入 node_timings                 │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 3  缓存层 (Cache)                                  │
│ .agent/embed_cache/{content_hash}.npy                    │
│ 触发：SemanticMemory.add()                               │
│ 策略：SHA256 content_hash → disk cache → numpy load      │
│ 统计：embed_cache_hit_rate 进 report                     │
│ 失效：模型切换时手动清理                                  │
└─────────────────────────────────────────────────────────┘
```

## Layer 1: 投影层

**代码位置**: `agent_runtime/context_manager.py:_get_knowledge`

每次 prompt 构建时，从 `episodic_notes`（会话级工具执行笔记）和 `durable topics`（跨会话 Markdown 文件）检索相关内容，注入 prompt 的 `knowledge` section。

```python
# 查询提取（三层共用）
query = derive_embed_query(user_message, task_summary)

# 双路检索
semantic_results = retrieval_candidates_semantic(state, query)  # cosine + keyword
keyword_results = store.retrieval(query)                        # exact match
```

**设计决策**: 为什么用双路而不是纯 semantic？
- Semantic 模型约 90MB，加载耗时 2-3s
- 如果模型不可用（离线环境），keyword 路径自动降级
- keyword 对精确匹配（文件路径、错误类型）更准确
- semantic 对语义相似（同义词、英文变体）更鲁棒

## Layer 2: 流水线

**代码位置**: `src/repair/precedent.py:RepairPrecedentStore`

在 Orchestrator 启动 repair 时，从历史修复先例中检索相似案例。先例以 JSON lines 格式存储在 `.agent/memory/topics/dependency-facts.md`。

```python
store = RepairPrecedentStore(repo_root)
similar = store.load_similar(
    issue_type="type_error",
    query=issue,        # semantic 过滤
    threshold=0.4,      # cosine ≥ 0.4
    limit=3,
)
# trace: state.node_timings["similar_fixes"] = similar
# trace: state.node_timings["precedent_score"] = max_cosine
```

**设计决策**: 为什么存在 Layer 2 而不合并进 Layer 1？
- Layer 1 服务于"当前任务需要什么上下文"（what）
- Layer 2 服务于"类似问题怎么修过"（how）
- 先例检索需要不同的数据结构（JSON 行 vs Markdown 段落）
- 分离后可以独立优化：Layer 1 优化召回率，Layer 2 优化精确率

## Layer 3: 缓存层

**代码位置**: `agent_runtime/features/memory/semantic.py`

SentenceTransformer embedding 计算是 RAG 的性能瓶颈（每文本 50-100ms）。通过 content_hash 缓存已计算的 embedding 到 `.agent/embed_cache/`，重复文本直接读磁盘。

```python
content_hash = hashlib.sha256(text.encode()).hexdigest()[:32]
embedding = _load_embed_cache(content_hash)  # hit → 零延迟
if embedding is None:                         # miss → encode + save
    embedding = model.encode(text)
    _save_embed_cache(content_hash, embedding)
```

**统计**: `SemanticMemory.embed_cache_hit_rate` 写入 `report.json`

**设计决策**: 为什么用文件系统而不是 Redis/memcached？
- 本地项目，不需要分布式缓存
- numpy `.npy` 格式零序列化开销（直接 mmap）
- 重启后缓存不丢失（持久化）

## 三层共用 derive_embed_query()

```python
def derive_embed_query(user_message, task_summary=""):
    """从 user request 提取 embedding 搜索关键词。
    规则: 异常类型 → 文件名 → 函数名 → task_summary fallback
    """
```

- Layer 1: `ContextManager._get_knowledge` 用此提取 query
- Layer 2: Orchestrator 用此构建 `similar_fixes` 查询
- Layer 3: SemanticMemory 用此做 chunk 检索

三层不在不同模块重复实现查询提取逻辑，统一走 `derive_embed_query()`。

## Trace 统一字段

| 字段 | 来源 | 含义 |
|---|---|---|
| `memory_retrieval_path` | Layer 1 | `semantic` / `keyword` / `degrade` |
| `precedent_score` | Layer 2 | 最高 cosine 相似度 |
| `embed_cache_hit_rate` | Layer 3 | hits/(hits+misses) |
| `episodic_hits` | Layer 1 | 命中的 episodic 条目数 |
| `durable_hits` | Layer 1 | 命中的 durable 条目数 |
