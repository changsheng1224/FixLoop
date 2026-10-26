# LangGraph 离线演示（D6）

`scripts/demo_repair_graph.py` 创建独立的两文件工作区，使用固定模型响应，经过正式
工厂和 `RepairService.run` 执行真实工具修改、Plan、回滚与 pytest。演示代码不导入
tests，不修改生产策略。它验证编排过程，不测量模型理解和修复能力。

## 运行

在仓库根目录，使用已安装 `.[dev]` 的同一解释器：

```powershell
python scripts/demo_repair_graph.py --scenario success
python scripts/demo_repair_graph.py --scenario retry
```

每次生成新的 `.tmp/lg-*` 目录并打印绝对路径，保留全部产物。可通过
`--output .tmp/my-graph-demo` 指定新目录；已存在的输出目录会被拒绝。host 模式只
修改演示目录；没有模型凭据、真实模型或 WSL/Docker 的前置要求。关闭远程 tracing
及 Hugging Face 联网加载，模型使用 FakeModelClient。

`success` 把返回值 1 改为 2，通过测试。`retry` 先提交返回值 3 的错误候选，真实
pytest 失败后沿 verification_failure 分支回滚，再通过原 Plan 重规划修改为 2。
终端出现基线或首轮 pytest 失败日志属于演示过程；最终以 report 和退出码为准。

| 产物 | 检查内容 |
| --- | --- |
| `report.json` | 真实终态、重试数、模型调用数、测试结果、Plan 版本与 owner 释放 |
| `repair.diff` | 工作区相对初始内容的最终补丁 |
| `events.json` | 原业务事件，包含尝试、工具、验证和 Plan 过程 |
| `flow.mmd` | 实际编译图导出的完整拓扑，不是本次执行轨迹 |
| `workspace/` | 实际源码和测试 |
| `state/.agent/` | 原 checkpoint、Plan SQLite journal、证据与协调记录 |

脚本检查 fixed、最终文件、重试数、Plan 完成与版本、写操作次数和 owner 释放，
不满足预期时以非零退出。事件只表达原业务事件，不冒充逐图节点的执行轨迹。

## 本轮实际结果

2026-10-26，Windows x64 / Python 3.14.3 / LangGraph 1.2.17，使用
`.runtime-tools/langgraph-d2/Scripts/python.exe`：

| 场景 | 终态 | 重试 | 模型调用 | Plan 版本 | 写操作 | 最终 pytest |
| --- | --- | --- | --- | --- | --- | --- |
| success | fixed | 0 | 4 | 1 | 1 | 1 passed |
| retry | fixed | 1 | 8 | 2 | 2 | 1 passed |

两个场景的 Plan 均 completed，协调状态 released，owner 均释放。模型调用数是固定
响应演示的实际计数，不是成本或效率提升的对照实验。retry 保留 `verify_logic`
失败归因作为历史诊断，不影响最终经过真实验证得到的 fixed。

本地保留成功产物于 `.tmp/d6-success`、重试产物于 `.tmp/d6-probe-retry`。
首个成功场景试跑在序列化报告时发现使用了不匹配的 `model_dump()`；修正为现有
`VerificationResult.to_dict()` 后复验。生产修复结果本身未受此演示脚本错误影响。
现有目录拒绝检查通过；脚本 Ruff lint/format 通过，文档本地链接与实际编译图
拓扑一致性检查通过。D6 没有变更生产运行逻辑，也没有重复运行 D5 已通过的测试矩阵。

讲解时先看 flow.mmd 定位分支，再对照 retry 的事件、最终补丁、Plan 版本与原日志。
进程退出恢复和权限故障使用 [D5 中的相关测试](LANGGRAPH_D5_ACCEPTANCE.md)，
这两个演示不覆盖原生沙箱审批或任意图断点恢复。
