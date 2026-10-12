# S3：缓存归属与失效

S3 面向本地单用户、多项目、并发修复。embedding 是可重建的派生数据：项目之间分区，同项目的 worktree、Workbench 运行副本及重启后的运行可复用；每个 Agent 的检索语料仍独立构建。

## 归属与路径

共享存储根由显式 `embedding_state_base`、`FIXLOOP_STATE_ROOT` 或平台默认目录确定。Agent 创建时冻结绝对路径，延迟加载模型时不再跟随 cwd 或环境变量变化。

```text
<shared-storage>/projects/<project_id>/
  caches/embeddings-v2/<cache-key>.npy   # 同项目共享的派生向量
  tasks/<run_id>/...                   # 各次运行的可变状态
```

`project_id` 沿用 S1 的规范化源项目路径身份。相同 remote/commit 的独立 clone 仍是不同项目；受管 worktree 和 Workbench 快照副本通过可信入口显式传递源项目根。文件内容变化不会改变项目身份，但实际编码文本变化会改变缓存 key。

`ToolContext.project_root` 表示源项目，`embedding_state_base` 表示共享存储根，`state_root` 表示运行状态位置。它们不能互相替代：Workbench 传入的 `state_root` 已经是 run 目录。repair factory 的共享存储参数约定保留，并提供独立的 embedding 存储参数供运行副本入口使用。

无缓存上下文的 `SemanticMemory()`、SkillCatalog 和 RepairPrecedent 辅助索引仅在进程内持有向量；不再根据 cwd 创建 `.agent/embed_cache`。旧缓存不读取、不迁移，调用时直接重建。

## 失效规则

`EmbeddingConfig` 是不可变的编码配置。最终文件名是以下描述符的完整 SHA-256：

| key 内容 | 解决的问题 |
| --- | --- |
| 实际送入 encoder 的文本 SHA-256 | 修改文本、裁剪、query 改写后的输入变化 |
| 模型 ID、不可变 revision | 同名模型更新后复用旧向量 |
| 最大 token 长度、归一化、向量维度 | 编码参数变化 |
| 分块大小、文本处理算法版本 | 分块与输入处理策略变化 |
| sentence-transformers / transformers / torch / NumPy 版本 | 编码实现升级 |
| 缓存格式版本、float32、CPU | 存储格式与执行配置变化 |

默认模型为 `sentence-transformers/all-MiniLM-L6-v2`，固定 revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`；加载时显式使用 CPU、关闭 remote code，编码显式使用 float32。更换模型必须提供不可变 revision，不能使用 `main` 或 `latest`。修改文本处理算法时需要同步更新 `text_version`。

短文本、长文本分块、查询均经 `SemanticMemory.embed()`。命中只复用向量；当前轮次的检索集合从本轮状态重新构建，不因命中而导入其他会话的 note、来源或权限。

## 并发与故障

每次写入创建同目录的独占 UUID 临时文件，写完后 flush、fsync，再原子 replace 发布。并发写入相同 key 可重复计算，读者只能读取完整的已发布文件。MVP 不添加分布式锁或单次计算保证。

读取使用 `allow_pickle=False`，要求一维 float32、维度匹配、所有值有限且向量非零。缺失、截断、错误 dtype/维度、NaN、零向量及 I/O 错误均按 miss 处理，重新编码后覆盖。缓存不可写时保留新计算的内存向量，检索继续工作。

Windows 并发替换期间可能暂时拒绝打开文件，此时同样按 miss 重算。目标文件和更长的 UUID 临时路径各自应用 extended-path I/O 转换；转换后的路径不作为持久化身份。共享状态目录创建后重新解析原始路径，避免并发创建时的暂时 `\\?\` 前缀进入任务身份。

进程强制退出可能留下未发布临时文件，下次读取不会使用它们。旧配置 key 和遗留临时文件的容量管理留给后续 GC；本阶段不实现配额、TTL、LRU 或后台清理。

## 边界与验证

项目分区提供应用层的归属约束。共享模型权重是公共不可变资源，仍使用 HuggingFace 模型缓存；模块级命中统计是汇总观测数据。S3 不实现多用户鉴权、操作系统级保密、记忆授权或通用缓存服务。

`tests/test_embedding_cache_isolation.py` 用确定性假 encoder 和真实 NumPy 文件验证项目/cwd 隔离、配置失效、损坏重建、写入中断、实际跨进程并发、长路径、worktree/沙箱恢复、Workbench 接线及缓存写失败后的检索。相关 Agent、repair factory、上下文、项目身份及 Workbench 回归测试验证调用链。

本次验证不下载模型、不调用远程 LLM；当前解释器未安装 sentence-transformers，真实模型编码性能与命中收益不在本次验证结论中。

## 面试解释

“我先确定资源归属：运行状态属于 run，embedding 属于源项目，同项目的修复副本可以共享向量。key 同时绑定实际输入和不可变编码配置，防止文本、模型或分块策略变化后误命中。写入通过独占临时文件和原子替换发布；损坏或读取失败就重算。复用向量后，检索语料仍从当前会话状态建立。这个版本解决本地多项目并发修复的缓存串用和失效问题，容量管理与多用户授权是后续阶段。”
