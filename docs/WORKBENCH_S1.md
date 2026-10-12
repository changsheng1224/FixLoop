# Workbench S1：会话树与持久化

S1 在 `src/workbench/` 提供独立的本地 SQLite 存储 API，使用 Python 标准库，
不启动模型、不执行修复、不读取或回退项目文件。它是后续工作台服务和界面的数据基础。
Layer 1 的线性 session、checkpoint 和 Layer 2 的 RepairState 保持各自职责；
本模块不导入或迁移历史运行时 session。

## 数据模型

| 对象 | 内容与约束 |
| --- | --- |
| Conversation | 标题、可选项目路径、初始快照、当前执行分支、浏览节点、revision |
| Branch | 名称、分叉点、末端节点；共享祖先，保留各自后续历史 |
| Node | 不可变的轮次请求或本地修改检查点；通过 parent_id 形成树 |
| Run | 每个请求节点唯一关联一次执行；状态、回答、错误、产物、执行后快照 |
| Snapshot | 不可变的外部存储引用及 JSON manifest；只记录引用，不保存文件内容 |

一个 turn 节点表示一轮用户请求，回答和任务结果存在对应 Run 中。
重新执行会创建新的节点和 Run，不复用旧执行记录。
本地修改检查点是独立节点，参与文件状态路径，不作为用户或助手消息发送。

项目路径在创建时规范化为绝对路径，此阶段不检查路径是否存在，也不将关联项目解释为
文件访问权限。会话创建后项目关联保持固定；项目管理与权限装配属于后续服务层。

## 分支语义

- `append_turn()`：接到当前执行分支的末端，与用户正在浏览的节点无关。
- `view_node()`：浏览任意历史节点；`None` 为根，不改变执行分支。
- `fork_at()`：从给定节点之后继续，创建并选中新分支；`None` 为从根开始。
- `edit_turn()`：在原轮次父节点处分叉，使用修改后的请求和原执行前快照。
- `regenerate_turn()`：在原轮次父节点处分叉，使用原请求、mode 和执行前快照。
- `switch_branch()`：选中已有分支末端，同时更新浏览位置。

旧分支的后续请求、回答、Run 和产物全部保留；新分支只包含共享祖先和新产生的内容。
`get_messages()` 只投影指定节点的祖先路径，排除兄弟分支和本地检查点。
完成或中断执行若有回答文本，会投影该文本；执行状态、错误和产物从 Run 单独读取，
不能把“有回答”解释为验证成功。该投影不复制运行时记忆、工具证据或摘要，
后续运行时接入仍须保证这些数据的分支隔离。

## 使用示例

```python
from src.workbench import WorkbenchStore

# 服务层应提供可信的状态目录，放在项目文件恢复范围之外。
store = WorkbenchStore("/path/to/app-state/workbench.sqlite3")
conversation = store.create_conversation("写作讨论")
original_branch = conversation.active_branch_id

turn = store.append_turn(conversation.id, "写一段项目介绍")
store.start_run(conversation.id, turn.run.id)
store.finish_run(conversation.id, turn.run.id, answer="项目介绍……")

# 编辑第一轮：新分支从空历史开始，旧回答继续保留。
revision = store.get_conversation(conversation.id).revision
edited = store.edit_turn(
    conversation.id,
    turn.node.id,
    "写一段面向开发者的项目介绍",
    expected_revision=revision,
)
assert edited.node.parent_id is None
assert store.get_messages(conversation.id, edited.node.id) == (
    {"role": "user", "content": "写一段面向开发者的项目介绍"},
)

store.start_run(conversation.id, edited.run.id)
store.finish_run(conversation.id, edited.run.id, answer="新介绍……")
store.switch_branch(conversation.id, original_branch)

# 独立进程也可打开同一文件，完整恢复树、执行状态和两个游标。
reopened = WorkbenchStore(store.database)
assert reopened.get_tree(conversation.id) == store.get_tree(conversation.id)
```

项目模式为 `project_chat` 或 `repair`，必须关联项目；`chat` 可用于任意会话。
这些 mode 在 S1 仅是元数据，真正的工具权限和执行路由属于 S2/S4。

## 初始快照与手动修改

项目第一次执行前，使用 `record_snapshot(..., as_baseline=True)` 记录初始快照。
只能在没有历史节点时设置一次，之后不能替换。根节点的有效文件状态就是该快照。

```python
project = store.create_conversation("项目讨论", project_path="/path/to/repo")
baseline = store.record_snapshot(project.id, "snapshots/S0", as_baseline=True)
turn = store.append_turn(project.id, "修复这个错误", mode="repair")
assert turn.node.input_snapshot_id == baseline.id
store.start_run(project.id, turn.run.id)

output = store.record_snapshot(project.id, "snapshots/S1", manifest={"files": ["app.py"]})
store.finish_run(project.id, turn.run.id, answer="修复报告", output_snapshot_id=output.id)

manual = store.record_snapshot(project.id, "snapshots/manual")
checkpoint = store.append_local_checkpoint(project.id, manual.id)
assert store.get_effective_snapshot(project.id, checkpoint.id) == manual.id
```

`append_turn()` 默认继承末端有效快照，也可显式传入 `input_snapshot_id`。
编辑或重跑必须使用原轮次的 input snapshot，不能使用当前分支末端的输出。
有效快照按本地检查点、Run 输出、节点输入、祖先、初始快照的顺序解析。
失败或取消的 Run 也可记录最终实际文件状态，不假定它们没有产生修改。

Snapshot 的 storage_key 是不透明引用，不能直接当作可信文件路径使用。
S3 负责创建真实快照、检测手动修改、确认恢复范围和执行恢复；
S1 的分支切换不会改动工作区，不能直接作为项目回退执行入口。

## 事务、并发与异常恢复

- 所有写操作使用 `BEGIN IMMEDIATE`，分支创建、节点、Run 和游标变更共同提交。
- WAL 支持读写并存；读树使用同一读事务，避免读取混合版本。
- 支持 expected_revision 的写接口会拒绝陈旧请求；失败事务不增加 revision。
- 同一会话存在 queued/running Run 时，禁止追加、分叉、编辑、重跑和切换分支。
  浏览、读取和记录快照仍可进行。跨会话共享同一项目的文件锁属于 S3。
- 状态转换：queued → running → succeeded/failed/cancelled/interrupted；
  queued 也可直接取消或标记 interrupted。只有一次 start 可以成功。
- 终态 Run 不能修改，历史节点和快照不能修改或删除；重新执行创建新记录。
- 数据库外键和 API 同时约束引用属于同一会话。
- 打开数据库不会自动中断执行。确认旧 worker 已停止并获得独占所有权后，
  服务层可调用 `interrupt_pending_runs()` 标记遗留执行。它不会重放工具，
  不证明工作区已恢复，后续执行前需要协调实际文件状态。
- Schema 只接受当前版本，拒绝未知版本和其他应用数据库，不执行自动迁移。

错误分类：对象不存在或跨会话引用为 NotFoundError；陈旧版本、忙碌状态和非法
执行转换为 ConflictError；不支持的数据库为 SchemaError；参数错误为 ValueError。
SQLite I/O、锁超时或损坏错误保留原始异常，由服务层呈现。

## 验证

```bash
python -m pytest tests/test_workbench_store.py -q --basetemp=.runtime-tools/pytest-workbench-s1
ruff check src/workbench tests/test_workbench_store.py
ruff format --check src/workbench tests/test_workbench_store.py
```

用例覆盖三轮历史编辑保留原分支、多步浏览与继续、重跑、快照输入输出与手动检查点、
独立 Python 进程重启恢复、并发写入与执行 claim、陈旧请求、失败事务和跨会话隔离。
