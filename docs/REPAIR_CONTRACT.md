# 正式修复契约

## 唯一入口和分层

所有调用端通过 `RepairService.run(RepairRequest)` 执行修复。CLI、Workbench、离线及真实评测只构造请求、展示结果；评测可独立判分。服务由 `src.repair.assembly.create_repair_service` 装配。已删除旧 Orchestrator、Planner、模型 Verifier 工厂和兼容入口。

阶段顺序为准备工作区 → 探索 → Patcher 修改 → 真实验证 → 输出结果。修复重试使用相同阶段规则。Patcher 必须调用受治理工具落盘，最后回答中的 diff/JSON 仅供展示，不会再次应用。

Layer 1 不导入 Layer 2。普通 `Agent` 使用通用执行循环、上下文、预算、取消和工具治理；修复提示、角色默认值、上下文投影、阶段规则由 Layer 2 注入。聊天流和任务调度器保持各自职责。

## 会话、服务和资源

`ExecutionSession` 持有工作区、运行预算、截止时间、取消令牌、事件及本次请求。`RepairCoordinator` 协调阶段；探索服务、Patcher 服务、补丁事务和 `VerificationService` 按显式会话接口合作，不访问其他对象的私有字段。

执行会话禁止并发运行。拥有资源的服务负责关闭资源；正常、异常和取消均释放编辑锁、心跳、预算绑定、探索执行器与后台后端。超时先取消，再等待执行路径返回，避免脱离资源所有者继续写入。不能确认进程退出或回滚时返回 `recovery_required`，阻止下一次写入。

Explorer 使用与 Patcher 相同的模型／工具执行核心，采用新会话、独立上下文和只读工具白名单 `list_files/grep/read_file`。它仅获得经范围检查的请求和证据，不继承主会话历史、写权限、进程执行、网络或委派能力。不完整或截断的发现不能当成成功证据。

Patcher 可经宿主显式启用 `web_search/web_fetch` 获取不可信公开资料；默认关闭，
工作区不能激活。网络工具使用运行请求／成本预算，不能产生本地源码读取或真实验证证据。
沙箱中使用独立 `trusted_network` 权限与固定宿主执行器，命令沙箱仍无网络。
配置、费用估算和限制见 [外部资料搜索与抓取](WEB_RESEARCH.md)。

## 补丁核心和后端事务

`agent_runtime.patch_engine` 是唯一纯文本核心。它在任何写入前计算所有输出，严格逐行检查上下文和删除原文；所有 hunk 坐标均对应原始文本。解析状态决定文件头与 hunk 内容，源码行 `---`、`+++` 不会被跳过。

`apply_patch` 接受标准多文件 unified diff 或严格 `*** Begin Patch` / `*** End Patch` 信封。`patch_file` 接受单文件 unified hunk 或恰好出现一次的 `old_text/new_text` 替换。信封里的无坐标 `@@` 可唯一定位原文；歧义或不匹配拒绝，不做模糊匹配。路径必须在工作区内。

| 输入语义 | 支持范围 |
| --- | --- |
| 文本编码 | UTF-8 文本；NUL／二进制拒绝 |
| 换行 | 一致 LF 或 CRLF；unified 修改保留原文件换行；混合换行和裸 CR 拒绝 |
| 无末尾换行 | unified diff 和信封 Update 使用 `\ No newline at end of file`，只允许用于文件最后一行；创建无末尾换行文件须使用 unified diff，信封 Add 追加末尾换行 |
| 空文件 | 创建／删除空文件；零行插入 hunk 支持 |
| 创建／删除 | unified diff 使用 `/dev/null`，创建要求不存在；删除 hunk 必须匹配并删除全部原文 |
| 信封删除 | `*** Delete File:` 删除快照时存在的文件；该语法不携带逐行旧文，后端检查提交前快照未变 |
| 多文件 | 所有目标先解析、验证，再提交；重复路径拒绝 |
| Git 元数据 | 常规 `diff --git`、`index`；创建／删除只接受 `100644` 元数据 |
| 不支持 | 二进制、rename/copy、移动信封、权限模式变更、combined diff、模糊上下文和超出声明行数的格式 |

后端负责权限检查、快照、内容版本和原子字节写入。主机、沙箱和评测使用相同核心。过期原文或格式错误不会留下修改；多文件提交中途失败会恢复已经写入的文件及新增目录。跨文件提交不等于操作系统级同时可见；不可确认恢复时明确报告恢复失败，不声称工作区未变。文件模式由后端保留，补丁不能改变权限。

## 有效限制

配置解析优先级从低到高为默认值、profile、用户文件、工作区文件、环境变量、显式请求参数。通过 `config.snapshot()` 查看最终值、逐字段来源和配置 hash。角色默认值集中在 `src.repair.policy.ROLE_DEFAULTS`，工厂不再增加角色固定配额。

单次上下文容量由 `prompt_budget`、`hard_cap`、模型窗口减输出预留共同决定。`budget.prompt_tokens/output_tokens/total_tokens` 是累计预算；`max_steps`、`max_model_attempts`、工具及模型调用次数、`deadline` 是独立限制。配置中的预算／deadline 值 `0` 表示不限额，权限白名单独立生效。停止结果报告具体资源和限额。

Explorer 显式安全上限：每子任务 3 次模型轮次、4 次工具、24000 token、60 秒；整个探索批次 96000 token；清理最多 2 秒、collect 最多等待 1000 毫秒。配置只能降低这些上限，超限直接拒绝。通用配置的安全边界声明于 Pydantic 字段（如输出最多 8192 token、并发 shell 最多 32）；超限同样拒绝，不静默截断。

`Retry-After` 是服务端最早重试时间，不经缩短等待的 jitter。没有该字段时用指数退避和随机抖动。等待检查取消与总截止时间；时间不足返回明确截止时间错误，不提前重试。

## 验证与结果

`VerificationService` 执行真实命令并判定结果。模型可以解释日志，不能替代验证。公开验证状态仅有：

| 状态 | 含义 |
| --- | --- |
| `passed` | 命令完成且实际测试通过，测试数大于零 |
| `failed` | 实际测试失败 |
| `environment_unavailable` | 环境／后端不可用或未能启动 |
| `timed_out` | 验证超时 |
| `cancelled` | 验证取消或中断 |
| `unverified` | 跳过、仅静态检查、无测试或未完成验证 |

只有满足请求的 `verification_target` 和 `verification_min_tests`，并且真实验证 `passed` 才可标记 `fixed`。静态检查、skip、dry-run、环境错误均不能构成修复成功。未确认资源清理优先于成功结果；取消、超时和失败语义在调用端一致。

## 版本和发布门禁

RuntimePolicy、RepairConfig、RepairState 和 VerificationResult 使用 `2.0`；通用 session 和 checkpoint envelope 使用 `3.0`；Workbench schema 使用整数 `3`。旧版、缺失版本和废弃字段明确拒绝，不迁移、不提供旧导出别名。历史结果只供人工审计。

评测资源使用 `importlib.resources` 定位，`.diff` 纳入 package-data。缺失必需资源明确抛出带路径的错误。`scripts/wheel_smoke.py` 从独立干净源码副本构建 wheel、安装到隔离目录，再从源码目录之外运行真实修复服务的离线 Case 冒烟，并验证模块来源。

PR 的 Linux/Windows 轻量门禁包括共享后端补丁、版本、预算、取消、Retry-After、Explorer 和流程一致性测试、lint、format 与 wheel 冒烟。全量、Docker、真实模型评测仍手动执行。
