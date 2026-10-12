# S4：记忆访问边界

S4 面向本地单用户、多项目、并发修复。资源有归属还不够：召回、直接按 ID 查看、修改、使用反馈和维护任务都必须检查同一访问上下文。范围判定先于关键词评分、语义编码和 prompt 投影。

## 可信身份与访问矩阵

`MemoryAccessContext` 是不可变调用上下文。运行时从 `ToolContext.project_root` 得到源项目 ID；沙箱使用经过校验的 binding task ID，host repair 和 Workbench 使用控制器绑定的 run ID。普通多轮聊天使用创建时固定的 session ID，允许在同会话内继续积累经验。它不从模型生成的 Candidate、session 内的 `memory_identity` 或召回参数中取得权限。

本地运行时 principal 固定为 `local`；现有治理 API 仍可用明确的 user ID 测试用户归属，但本阶段没有登录、令牌验证或多租户身份服务。

| 记忆范围 | 允许读取 | 允许修改 |
| --- | --- | --- |
| run / task | 同项目、同 principal、同运行；普通聊天为同会话 | 相同归属的本地记录 |
| repository | 相同项目和 principal；共享目录仅提供 durable 记录 | 运行只能修改本地记录；共享读取上下文拒绝修改 |
| repository_version | 在上述项目边界内，还必须匹配当前内容版本 | 本地所有者可重新验证；共享读取上下文拒绝修改 |
| user | 当前项目、本地主体的运行存储内 | 本地所有者；不自动形成跨项目用户画像 |
| 缺失归属 / 未知 scope | 不召回、不投影 | 不接管、不自动补全历史记录 |

范围检查覆盖 recall、inspect、list、confirm、reject、forget、pin、证据绑定、验证、usage 反馈、冲突处理和 Dream 维护。召回的可选 user/task 参数只能缩小范围，空 task 不会放开边界。记忆 ID 包含项目、principal、scope 和适用的 task 命名空间，模型指定的同名 ID 也不能覆盖另一个 run 的记录。

`CanonicalMemoryStore` 是可信底层持久化组件，不承担鉴权；生产调用通过治理服务。目录分区和数据库 CAS 分别解决存储归属与版本冲突，不能代替访问检查。

## 本地候选与共享项目知识

默认普通 Agent 的治理记忆位于共享外部存储的项目/session 分区；绑定修复 Agent 使用已经确定的 run 状态根。

```text
<shared-storage>/projects/<project_id>/
  tasks/<run-or-session-id>/.agent/memory/memory.db
  knowledge/.agent/memory/memory.db
```

Workbench 使用其独立的 worker/run 状态根存放本地记忆，同时沿用源项目的 `knowledge` 分区。不同分支的历史仍由祖先链提供，不通过项目 ID 自动合并。

普通修复结束和 Dream 只整理本地候选、证据及质量；即使本地条目的状态变成 durable，也不会自动复制到项目共享目录。项目共享知识通过应用层 `publish_project_memory(agent, memory_id, user_confirmed=True)` 发布。该函数没有注册为模型工具，也没有本阶段的 HTTP/UI 发布入口。

发布前检查本地所有权、版本及冲突状态。run/task 候选会创建一个 repository 记录，保留 `parent_memory_id` 与可信 `source_run_id`；原记录保持运行范围。用户画像不会通过该 API 发布。缺少明确确认或共享存储不可写时直接报错，避免返回虚假的发布成功。

共享阅读上下文只读，不允许当前修复的 confirm、forget、usage 反馈或 Dream 修改历史知识。模型检索到知识，并不获得修改该知识的权限。

## 版本与旁路

版本限定知识使用既有 bounded inventory 的文件内容版本，排除 Git/运行状态及依赖缓存等既有噪声目录。它与 worktree 绝对路径和临时分支名无关：同内容的执行副本能匹配；修改源码、新增或删除文件会改变版本。只有访问版本限定记录时才读取清单，每次治理服务实例内复用结果。无法取得版本时不召回该类知识。

host 读取当前执行目录；sandbox 通过既有 guarded workspace inventory 获取版本，不直接读取宿主源码。这里的版本覆盖既有 inventory 策略范围，并非文件系统的原子快照；并发外部编辑和读取之后发生的变化仍需当前工具证据、预条件及验证处理。

Markdown durable store 和用户偏好辅助 API 保留为底层数据工具，原始 Markdown 不再单独进入生产 prompt。旧无归属记录不自动迁移或据目录补上权限。成功修复写入的 RepairPrecedent 留在当前 run，读取先检查项目/principal/task；未知或其他运行的文件不能被覆盖。

恢复 session 时校验存储对应的执行目录与已记录的源项目。切换交互项目会清空记忆状态、待处理候选与语义语料，并生成新的会话记忆边界；沙箱恢复继续以 binding 身份访问同 run 的记录。

## 故障与验证

本地存储不可用时治理服务仍维护当前运行的内存记录，并设置 `memory_persistence=memory_only`；不会回退到另一个项目目录。默认路径解析不创建外部目录，真正持久化由 store 完成。明确发布到项目知识的失败会返回错误。

`tests/test_memory_access_boundaries.py` 覆盖跨项目/principal/run 拒绝、空身份、按 ID 修改和反馈、命名空间、维护任务、旧数据、版本失配、显式发布及共享只读、Markdown 旁路、先例归属、存储失败、同会话恢复和沙箱 binding 恢复。相关回归覆盖治理、冲突、反馈、上下文、Agent loop、checkpoint、Workbench 和 repair factory。

测试使用 fake 模型及本地存储，不调用远程 LLM、不要求真实 WSL 演示。只执行相关测试和 lint，没有运行全仓测试。

## 面试解释

“缓存是可重建的派生向量，记忆则会影响决策，因此需要独立的访问边界。我从控制器构造项目和运行身份，先过滤归属与版本，再做检索和投影；按 ID 修改、反馈和后台维护也走相同检查。一次修复的候选默认留在本次运行，跨运行共享必须由用户明确确认发布，而且检索到共享知识不获得修改权。这个版本覆盖本地多项目并发修复；多用户认证、全局用户画像和知识管理 UI 留到后续阶段。”
