# 验证执行与补丁事务

`src/repair/verification/service.py` 统一选择基线、补丁验证和失败复查的执行层。
显式 `static` 只做静态检查，显式 `container` 只调用容器；容器暂不支持的语言返回环境失败。
`auto` 可以在首次验证时选择允许的降级层，一旦确定，后续阶段沿用该层。

`agent_runtime/test_execution.py` 是生产验证与评测共用的宿主机命令服务。
Windows 使用先挂起启动、再归属 Job Object、最后恢复执行的顺序，消除子进程归属竞态。
正常退出、取消和超时均清理进程范围；只有确认清理后才能回滚。POSIX 的进程组
无法证明 `setsid()` 脱离的后代已经停止，因此取消和超时在终止进程组后仍保留恢复阻塞，
不把进程组为空报告为进程树停止。需要完整隔离的执行应使用容器或 `wsl_bwrap`。

`src/repair/execution/lifecycle.py` 管理工作区、启动、取消及资源生命周期；
`src/repair/execution/transaction.py` 管理补丁应用与回滚。
`RepoSnapshot` 保存受管理文件的字节、模式、修改时间和符号链接目标，同时提供文本投影供补丁导出。
回滚删除新增的受管理文件、恢复删除或修改的文件，并核验恢复后态；忽略目录和显式范围外文件不参与回滚。
无法读取快照、无法恢复后态或无法确认执行停止时，不能宣告安全回滚。

验证收据明确记录 `mode`、实际执行层、执行完成状态、退出码和检查步骤。
动态验证提供真实测试数量；无法解析数量时不伪造一项通过测试。
Java Maven/Gradle 优先读取本次运行新生成或更新的 JUnit XML，Go 使用 JSON 测试事件，
JavaScript/Rust 等使用测试框架汇总。

Plan 的 `tests_passed` 要求测试通过且测试数量大于零；`static_checked` 要求静态检查实际完成且通过。
静态检查通过可完成 Plan 验证节点，修复运行的终态为 `pending_verify`；CLI 和报告显示
“静态检查通过，尚未运行测试”。执行未知保持 `uncertain`/`recovery_required`，不会按静态成功处理。

结构调整默认允许进入评审和验证。需要保护接口时，在 `RepairConfig` 中明确配置文件及限定名称：

```python
RepairConfig(protected_interfaces={"service.py": ["Service.run", "public_api"]})
```

这些接口的定义、同步/异步类型、参数、默认值、返回注解、装饰器和类继承声明受到保护。
新增辅助函数和未列入保护范围的结构调整不受定义集合限制。签名检查不宣称语义等价。

沙箱业务工具通过 `ToolContext.sandbox_trusted_handlers` 由 Layer 2 的工具工厂注册。
运行时保持权限门禁，并拒绝未注册的处理器；模型可见注册表里的任意回调不能替代可信处理器。
