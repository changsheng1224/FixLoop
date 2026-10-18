# 外部资料搜索与抓取

FixLoop 可通过 DeepSeek 独立 Search/Fetch API 查询公开文档、版本说明和 Issue。
推理模型不必使用 DeepSeek。Layer 1 提供传输与通用工具；Layer 2 只向 Patcher
授权，Explorer 仍仅能读取指定范围内的本地源码。此能力默认关闭。

## 启用

在启动 FixLoop 的宿主 PowerShell 中设置：

```powershell
$env:FIXLOOP_WEB_ENABLED = 'true'
$env:DEEPSEEK_API_KEY = '<your-key>'
$env:FIXLOOP_BUDGET_MAX_WEB_REQUESTS = '8'
```

也可在用户文件 `~/.fixloop/config.json` 中配置非凭据字段：

```json
{
  "schema_version": "2.0",
  "web": {"enabled": true, "timeout_s": 20, "max_attempts": 2, "preview_chars": 12000, "summary_chars": 700},
  "budget": {"max_web_requests": 8}
}
```

配置来源和生效值进入 `config.snapshot()`；API Key 只从宿主环境获取，不进入配置、
模型工具参数、checkpoint 或沙箱。工作区文件中的 `web` 段不参与配置合并，不能让
被修复仓库自行激活联网。程序调用 `create_repair_service(..., runtime_overrides=...)`
可显式设置上述字段。环境变量还有 `FIXLOOP_WEB_TIMEOUT_S`、
`FIXLOOP_WEB_MAX_ATTEMPTS`、`FIXLOOP_WEB_PREVIEW_CHARS` 和 `FIXLOOP_WEB_SUMMARY_CHARS`。

## 工具行为

- Patcher 先读取本地实现、失败证据和依赖声明，仅为明确的外部知识缺口联网。
  每次 Search/Fetch 必须填写 `purpose` 说明疑问；已知官方 URL 时优先 Fetch。
  通用 Layer 1 工具不强制填写修复角色的调用理由。
- `web_search(query, purpose, count=3, need_content=false, domains=[], target_version=...)`：
  count 为 1～50，默认 3。`domains` 是最多 5 个纯域名，不接受 URL、路径或通配符。
  目标版本和域名会拼入 query；拼接后不能超过服务端 128 字符限制，超限报错而非截断。
  返回结果再次按精确域名或其子域校验，拒绝凭据／非公开 URL，去除 fragment、跟踪参数
  和相同 ref 的 GitHub raw/blob 镜像重复项，保留供应商顺序。该范围只是调用者指定的
  检索约束，不意味着 FixLoop 已验证域名属于官方。默认摘要每条最多 700 字符，
  正文和完整摘要不进入紧凑预览，原始响应仍可展开。
- `web_fetch(url, purpose, focus=..., target_version=...)`：经 DeepSeek 获取标题和 Markdown。
  拒绝凭据 URL、本地/私网 IP、localhost 及明显内部主机名；不会在宿主直接访问目标 URL。
  目标站点解析、重定向和访问策略由 DeepSeek 服务端执行，不支持登录态或私有站点。
  `focus` 是关注的接口或行为，按关键词选择最多三段互不重叠的原文窗口并附偏移量；
  未命中时明确标记 `no_focus_match` 并展示正文开头。选段保留转义、缩进和代码字符，
  不尝试猜测或修复供应商已丢失的代码格式；选段边界可能落在代码块中，不能直接复制执行。
- `purpose`、`focus` 不发给供应商；`target_version` 仅作为搜索词和核验目标。
  版本适用性始终标记 `unverified`，Patcher 必须自行核对来源身份、版本范围及代码示例。
  `authority` 和 `rerank_score` 不视为技术正确性或版本适用性的证明。
- 固定请求目的地为 `https://api.deepseek.com/v1/tools/{search,fetch}`，不接受模型或
  工作区覆盖 endpoint；HTTP API 重定向拒绝跟随，避免 Authorization 泄露。
- 响应最多读取 2 MiB，默认展示 12000 字符预览；超长结果保存在受治理 Observation 中，
  模型可用 `expand_observation` 和返回的 `OBS-*` 引用分段展开。
- 预览、归档原文和 provenance 都保留不可信外部资料标记。页面指令不能授权操作，
  页面中的代码/测试日志不产生本地源码、读取前置条件或真实测试通过的证据。
  不应向搜索服务发送私有源码、凭据或私有 URL。
- 网络读取计入现有探索窗口。进入收敛／强制补丁决策时，原生工具视图隐藏 Search/Fetch，
  文本工具请求也由 preflight 拒绝。低相关、重复和版本不符的资料应舍弃；无有效来源时
  停止重复搜索，回到本地证据，必要时通过 `finish_repair` 说明仍缺少的知识。

调用示例：

```json
{"query":"TaskGroup cancellation","purpose":"核对取消后的异常传播规则，本地依赖为 Python 3.11","domains":["docs.python.org"],"target_version":"3.11"}
```

```json
{"url":"https://docs.python.org/3.11/library/asyncio-task.html","purpose":"核对目标版本的 TaskGroup 行为","focus":"TaskGroup CancelledError ExceptionGroup","target_version":"3.11"}
```

## 沙箱权限

命令沙箱继续禁止网络。Search/Fetch 走固定宿主实现，使用独立的 `trusted_network`
权限和现有精确调用审批、取消、所有者 fencing 与权限收据。默认 `PermissionScope`
不包含此权限：首次调用按运行中的审批策略申请，`never` 拒绝，`ask` 可暂停等待审批，
`auto` 使用现有权限系统授予范围。启用配置不会绕过审批。可撤销权限；可变工具注册表
和 `sandbox_trusted_handlers` 不能替换固定网络执行器。Dry-run 不发送请求。

## 配额、超时与费用

每个实际 HTTP 尝试（含失败/重试）占 1 次 `budget.max_web_requests`，默认每运行 8 次；
`0` 沿用运行时惯例表示不限额。逻辑工具调用仍占既有 read 和 tool 配额。
配额沿用运行 BudgetManager，可随现有预算 checkpoint 恢复。

一次工具操作默认总计最多 20 秒（含排队/退避/传输），同时受工具和全局 deadline 限制。
取消或超时后不启动后续重试；已经发出的只读 HTTP 请求可能在服务端继续完成，
其费用保守按可能已计费记录。传输线程不读取可变运行上下文，也不在稍后结算另一次运行。

408、429、5xx 最多按 `web.max_attempts` 尝试，`Retry-After` 是不可缩短的等待下限；
等待无法装入剩余时间时直接结束。其他 4xx、协议错误不重试。传输失败保守结束，
避免因不能确定服务端是否处理而自动重复。整个 Python 进程按接口分别以约 4.76 QPS
平滑调度；跨进程/跨机器账号限速及 Fetch 域名限速仍由服务端 429 控制。

工具 metadata 记录 provider、trust、HTTP 状态、请求次数、耗时和 `estimated_cost_usd`。
`web_research` 记录调用目的、目标版本和待核验状态，成功时还记录域名范围、过滤／去重／
摘要缩短计数或正文来源范围；保存到 Observation provenance，并产生与 OBS 引用绑定的
`web_research_observed` 事件。这些记录用于诊断资料使用情况，不证明资料正确或修复有效。
按文档美元费率，每次请求预留 $0.00075 并受运行 hard cost 上限约束；明确失败和
Search 空结果释放费用预留，未知计费状态保留。soft cost 只记录并触发既有降级，
不会阻断请求。费用是估算，账单以 DeepSeek 为准。

官方文档（2026-10-18 核对）：
[Search/Fetch 使用指南](https://api-docs.deepseek.com/zh-cn/guides/search/)、
[价格](https://api-docs.deepseek.com/quick_start/pricing/)。
调用费用与模型 token 消耗分别产生，不应将宿主应用中已有的服务端工具优惠理解为
FixLoop 直接调用这些接口也免费。
