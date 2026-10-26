# LangGraph 依赖与执行方式验证（D2）

日期：2026-10-26。前置：[D1 边界与行为矩阵](LANGGRAPH_ORCHESTRATION_BOUNDARY.md)。
本交付只增加可选依赖、实验程序和定向 CI；未修改正式修复入口或执行内核。

## 决策

后续阶段图采用 LangGraph 的公开同步 `invoke` / `stream(..., stream_mode="updates")`
接口。图调度放在独立线程；每个需要访问业务状态、执行阶段服务、提交上下文或
发布业务事件的调用，通过一个同步 owner 调用桥交回原调用线程执行。

调用桥只传递回调及结果／异常，不决定业务阶段，不拥有业务 State、预算、资源
或恢复协议。业务服务仍在原线程上运行；它们更新的 ContextVar 保留到下一阶段。
图通过临时 `next_action` 路由，runtime context 传入同一个 session／state 和调用桥。

```text
原调用线程（创建 PlanSession、保留业务上下文）
  ├─ 等待图请求 → 执行阶段回调 → 返回结果／异常
  └─ 图返回且工作线程退出后 → 原有异常处理与 finally 清理

图调度线程／框架 worker
  └─ 节点 → owner.call(阶段服务) → 条件边 → 下一节点
```

实验实现位于 `tests/langgraph_execution_support.py`，并未作为生产接口发布。
D3/D4 接入时应按本验证过的边界实现正式调用入口，不能从生产模块导入 tests。
真实服务调用保持串行；原有内部只读探索并发仍由执行内核管理。

## 为什么选择调用桥

| 方案 | 判断 |
| --- | --- |
| 直接在同步图节点执行原服务 | 不采用：框架可能切换线程；串行执行的快速路径不是 owner 线程契约 |
| async 节点内同步调用原服务 | 不作为本轮入口：会阻塞事件循环，且同步公开入口的事件循环衔接需额外设计 |
| 图调度与 owner 阶段调用分开 | 本轮实测方案：兼容同步调用和 updates 流，保留原 owner 线程及业务 ContextVar |

不替换框架 executor，不使用 `_runner`、`_executor` 或其他私有 API，不放宽
PlanSession 的 `owner_thread_required` 校验。额外强制线程池边界的反例测试证明：
直接跨线程提交被拒绝，而通过调用桥提交在原 owner 上成功。

调用桥在阶段服务抛错时传回同一异常，并等图调度线程结束后再返回；退出后拒绝
继续提交。用户取消沿用原 CancellationToken。调用桥不设置短等待超时来提前
放弃仍在执行的阶段，也不通过 daemon 线程让写操作脱离资源所有者。
测试同时覆盖同步调用者已处于 asyncio 事件循环的情形，不创建嵌套事件循环。

## 依赖选择与安装

`pyproject.toml` 新增可选 extra：

```toml
orchestration = ["langgraph==1.2.17"]
```

本轮只验证这个精确版本，不将结果扩展为全部 1.x。实际解析版本：

| 包 | 本次版本 |
| --- | --- |
| Python | 3.14.3，Windows x64 |
| langgraph | 1.2.17 |
| langchain-core | 1.7.0 |
| langgraph-checkpoint | 4.3.0 |
| langgraph-sdk | 0.4.9 |
| Pydantic | 2.13.4 |

`langchain-core` 等由 LangGraph 传递安装，不表示迁移现有模型／工具接口。
checkpoint 包虽然随框架安装，但实验图直接 `compile()`，不配置持久化。
版本上限不靠推测放宽；升级后需重新运行 owner 调用验证。
本仓库没有新增全局依赖锁文件，上表记录本次解析栈，CI 会再次验证其实际解析结果。

复现环境使用工作区内 `.runtime-tools/langgraph-d2` venv，复用已有系统测试依赖；
额外包及 editable 安装均写入 venv，不修改全局 Python。首次离线安装 extra 发现
既有 dev 依赖 pytest-xdist／wheel 未安装；补齐后重新验证项目声明的安装路径。

```powershell
python -m venv --system-site-packages .runtime-tools/langgraph-d2
.runtime-tools/langgraph-d2/Scripts/python.exe -m pip install -e ".[dev,orchestration]"
$env:LANGSMITH_TRACING='false'
$env:LANGCHAIN_TRACING_V2='false'
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
.runtime-tools/langgraph-d2/Scripts/python.exe -m pytest tests/test_langgraph_execution.py -q -p no:cacheprovider --basetemp=.runtime-tools/langgraph-d2/pytest
.runtime-tools/langgraph-d2/Scripts/python.exe -m pytest tests/test_repair_flow_parity.py -q -p no:cacheprovider --basetemp=.runtime-tools/langgraph-d2/pytest-parity
.runtime-tools/langgraph-d2/Scripts/python.exe -m pip check
```

安装需要网络；测试使用确定的 FakeModelClient，文件、SQLite、补丁和子 pytest
都真实执行，不需要模型服务、LangSmith 账号、Docker 或 WSL。

## 本地验收

| 检查 | 结果／覆盖 |
| --- | --- |
| 新增 D2 测试 | 8 passed；invoke 与 stream 分别执行真实补丁和实际 pytest |
| 线程与上下文 | patch／verify 均在 Plan owner；前一阶段的 ContextVar 更新在后一阶段可见 |
| 执行事实 | 实际文件为修复后内容，patch_file 操作恰好一次，真实测试数为 1，Plan 完成并保留原 checkpoint seal |
| 强制跨线程反例 | 原 Plan.prepare 拒绝非 owner；桥接后执行／用户消息提交成功 |
| 异常与取消 | 原异常对象传回，阶段 finally 已执行；原取消令牌传到正在运行的阶段 |
| 生命周期边界 | 结束后拒绝晚到调用，拒绝嵌套启动另一条 owner 调度 |
| 既有公开流程 | `tests/test_repair_flow_parity.py`：22 passed，覆盖 fresh／resume 等原行为 |
| 安装声明 | `.[dev,orchestration]` editable 安装成功；用隔离导入模式核对安装元数据包含精确版本 extra |
| 依赖一致性 | 同一解释器 `python -m pip check`：No broken requirements found |
| 导入边界 | 导入 Agent 与 RepairService 后，sys.modules 不包含 langgraph 模块 |
| Ruff | 新增两个 Python 文件 lint 与 format 检查通过 |

`.github/workflows/test.yml` 在原 Linux／Windows、Python 3.11 的轻量门禁中新增
extra 安装和 D2 测试。该配置尚未在远端执行，本次不声称 Linux／Python 3.11
实跑通过；本地仅验证 Windows／Python 3.14.3。未运行全量测试。

在完成 extra 安装后的最终环境中合并执行两个文件，结果为 **30 passed**。
本地 JUnit 记录为 `.runtime-tools/langgraph-d2/validation.xml`（忽略的验证产物）。
安装元数据核查使用 `python -I`，避开工作区已有旧 egg-info 对已安装元数据的遮蔽。

## 门禁结论与后续限制

D2 的本地线程接入门禁通过，可以据此开展 D3。正式迁移仍需验证全流程副作用
顺序、审批恢复、进程级崩溃、owner 接管、清理未知和各调用端事件，不以本次
两节点实验代替 D4/D5。调用桥退出后的资源终结仍由原生命周期负责。

本交付不承诺异步非阻塞入口、任意节点持久化恢复、图层并行写或性能提升。
若后续要直接调用 `ainvoke`、增加图级超时／并发或更换流式 API，应先扩展 D2
验证矩阵；不能假定本次同步 owner 桥的结论自动覆盖这些方式。

参考：[Graph API 与 runtime context](https://docs.langchain.com/oss/python/langgraph/graph-api)、
[PyPI 版本元数据](https://pypi.org/project/langgraph/1.2.17/)。
