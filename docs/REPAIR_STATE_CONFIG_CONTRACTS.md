# Repair 状态与配置契约

## 状态

`RepairState` schema 为 `1.2`。取消、超时、编辑范围、验证结果控制、
失败决策、止损和 Plan/探索恢复检查点等核心控制字段存放在
`state.control: RepairControl`。模型校验字段和赋值，未知字段会报错。
`patcher_write_attempted=None` 表示未观察到写入统计；`False` 表示已确认未尝试写入。

`node_timings` 保留耗时、计数、诊断和证据投影。已经定义为控制字段的键
禁止再存入该字典。状态导出和控制快照不会共享可变列表、字典。

持久化的 `1.0`/`1.1` 状态在加载时显式迁移控制字段；未知版本会拒绝。
检查点先核验原始 envelope、身份和 checksum，再迁移并校验控制模型。
新一轮恢复保留当前 owner 的取消/协调状态，并清除上一轮超时标记；
保存的恢复展示结果不能授权当前 owner 继续执行。

## 终态

`resolve_terminal_status` 是收尾、CLI 和报告的共同判定入口。
优先级为恢复未确认、用户取消、超时，再按修复结果、回归和止损/重试预算处理。
错误消息文本不会决定超时状态。取消和恢复未确认也不会被残留验证失败覆盖。

CLI 保持现有退出码：可交付补丁 `0`、失败 `1`、配置错误 `2`、超时 `3`。
报告仍要求实际运行测试且通过；静态检查、跳过验证和 dry-run 不构成已验证修复。

## 修复执行与恢复

首次执行与有效检查点恢复共用启动、修复回合和收尾逻辑。恢复只跳过 Issue
解析、种子定位和基线测试，并恢复已保存的黑板；阶段预算、取消检查、Critic、
启用验证时的 AST 签名检查、失败止损和进度事件使用同一条执行路径。

验证失败先回滚并更新诊断、相关测试和失败账本，再构建下一轮反馈。
错误日志作为公开证据保留，运行时不依据特定错误文本指定补丁实现。
`pending_verify` 与 `fixed` 均结束于 `done` 阶段，但前者仍表示尚未验证。

正常和异常退出都会关闭 Plan binding、停止进度心跳并释放本次执行持有的
编辑锁；锁按其注册的仓库根目录清理，不能清除其他 owner 的锁。

## 配置

统一优先级由低到高为：默认值、profile、用户文件、工作区文件、环境变量、显式参数。
用户文件默认是 `~/.fixloop/config.json`；工作区按 `.fixloop/config.json`、
`.agent/config.json` 顺序读取，后者优先。工作区不能激活外部代码探索服务。

预算和超时的旧标量名称与对应命名空间字段在每个来源内先归一化，再合并来源。
同一来源提供两种名称时，命名空间值优先；显式 `0` 仍有覆盖效力。
`prompt_budget` 是单次请求预算，`budget.prompt_tokens` 是整轮累计预算，两者独立。
显式空 `env={}` 禁用环境覆盖。非法 JSON、非对象配置、未知字段、无效数值和布尔值会报错。

Layer 2 配置写在共享文件的 `repair` 对象中，例如：

```json
{
  "model": "deepseek-v4-pro",
  "repair": {
    "sandbox_policy": "preferred",
    "patcher_max_steps": 24,
    "patcher_compact": true,
    "progress": true,
    "progress_stdout": true,
    "progress_heartbeat_s": 60
  }
}
```

现有 `FIXLOOP_SANDBOX_POLICY`、`FIXLOOP_PATCHER_MAX_STEPS`、`FIXLOOP_PATCHER_COMPACT`
以及 `FIXLOOP_PROGRESS*` 环境变量使用同一加载器。步数范围为 `1..50`，
心跳间隔至少 `5` 秒，不再静默修正错误输入。
没有显式 repair 步数时，Patcher 使用已解析的 Agent 步数配置。

Patcher/Verifier 角色默认值先于用户配置；角色 JSON 输出格式仍由角色契约决定。
模型 client、预热上下文、共享预算和 Agent 使用同一模型/provider 配置。
配置快照记录有效值、每个字段来源及 hash；凭据不进入配置模型和快照。
Layer 1 只提供通用来源合并器，Layer 2 定义自身配置，不增加 Layer 1 的业务模型依赖。

## 测试基础设施

`tests.repair_support.build_repository` 在调用者指定的 pytest 临时目录创建 UTF-8
仓库文件，可选建立已提交 Git 基线，Git 命令失败会直接暴露。
共享夹具限制 Git 向上查找，避免非 Git 测试目录继承开发仓库的源码、文档和状态。
定位用例使用独立临时根目录，取消全局索引缓存清空；恢复夹具保留稳定 run_id 和目标。
