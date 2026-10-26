# LangGraph 定向回归与故障验收（D5）

日期：2026-10-26。基线为 [D1 行为矩阵](LANGGRAPH_ORCHESTRATION_BOUNDARY.md)，
实现见 [D3/D4](LANGGRAPH_D3_D4_ACCEPTANCE.md)。D5 增加故障验证与安装断言，
没有改动生产状态、持久化或执行策略。

## 故障覆盖

`tests/test_repair_graph_faults.py` 新增 11 个参数展开后的用例：

| 注入点 | 实际断言 |
| --- | --- |
| patch / verify 方法完成后 `os._exit(81)` | 子进程真实退出；重新构造公开服务恢复；接纳已写补丁；写操作总数为 1；已完成验证不重复执行；恢复模型调用为 0 |
| Critic 完成后取消 | 验证不启动；返回前文件回滚；资源清理确认、owner 与锁释放 |
| 总超时的 timer 回调 | 阶段仍在执行时锁和原预算保持绑定；取消退出后才释放；最终 timeout |
| 图调度异常 / 图线程启动失败，fresh / resume | 原异常映射、心跳和锁清理、请求上下文释放 |
| L2 审批暂停后 fresh / resume 再进入 | 原 checkpoint 保存审批身份；原公开入口恢复；图正常结束 |
| 无执行回执的任意阶段暂停 | 保留未确认资源与 recovery_required，不能用审批异常伪造安全退出 |

审批覆盖分两层：新用例验证 L2 调度与恢复入口，`test_sandbox_permissions.py`
验证原 PermissionService 的精确操作审批、拒绝、撤销、身份绑定、owner 接管和 Plan
等待恢复。后者使用模拟 backend；本次未将其表述为真实 WSL/bubblewrap 验收。

初次故障批次为 8 passed / 1 failed：任意阶段抛审批异常时，内核仍有 planned /
unverified 资源，实际正确返回 recovery_required。修正测试期望并补充 L2 审批入口
用例。新入口用例还修正了测试夹具的空 run_id 和新执行/恢复身份互斥配置；按失败
nodeid 复验，没有修改生产协议或重跑已通过的整批故障测试。

## 复现

同一解释器安装与执行；本机为 Windows x64 / Python 3.14.3 / LangGraph 1.2.17。
标准开发环境先运行 `python -m pip install -e ".[dev]"`，再运行下列相关范围。
本次实际解释器为 `.runtime-tools/langgraph-d2/Scripts/python.exe`。

```powershell
$env:LANGSMITH_TRACING='false'
$env:LANGCHAIN_TRACING_V2='false'
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
python -m pytest tests/test_repair_graph_faults.py -q -p no:cacheprovider --basetemp=.tmp/d5-faults
python -m pytest tests/test_langgraph_execution.py tests/test_repair_graph.py tests/test_repair_graph_entry.py tests/test_repair_flow_parity.py tests/test_plan_l2_binding.py tests/test_resume_repair.py tests/test_verification_transactions.py tests/test_phase_c_timeout.py tests/test_run_coordination_binding.py tests/test_recovery_cancel_outcome.py tests/test_sandbox_permissions.py -q -p no:cacheprovider --basetemp=.tmp/d5-matrix
python -m pytest tests/test_cli_repair.py tests/test_repair_factory.py tests/test_eval_runner.py tests/test_plan_replan_binding.py tests/test_run_entry_isolation.py -q -p no:cacheprovider --basetemp=.tmp/d5-entry
python -m pytest tests/test_workbench_project.py::test_real_l2_repair_edits_disk_and_runs_local_verification -q -p no:cacheprovider --basetemp=.tmp/d5-workbench
python scripts/wheel_smoke.py
python -m pip check
python -m ruff check agent_runtime src tests scripts/wheel_smoke.py
python -m ruff format --check agent_runtime src tests scripts/wheel_smoke.py
```

故障测试已加入 CI 的 Windows/Linux Python 3.11 轻量图测试步骤，使用短 basetemp
避免 Windows 工作区深路径影响。远端 CI 尚未执行。

## 安装与范围

本轮实际结果（不把 D3/D4 历史通过数重复计入）：

| 范围 | 结果 |
| --- | --- |
| 阶段图、owner、行为一致性、Plan、恢复、事务、超时、协调、权限矩阵 | 179 passed |
| 新故障用例，含精确复验 | 11 个不同用例最终通过 |
| CLI、factory、EvalRunner、Plan 重规划、工作区隔离 | 68 passed / 1 个既有 skip |
| Workbench 真 L2 修改与本地验证 | 1 passed |
| 合计 | 259 个不同用例通过 / 1 skipped |

既有 skip 为 `TestCliRepair::test_repair_skip_verify_smoke`，测试本身声明跳过。
原始 JUnit 位于本地忽略目录 `.runtime-tools/langgraph-d2/d5-*.xml`；失败记录保留，
审批入口最后的两个参数用例结果在 `d5-wb.xml`。不提交包含运行目录的机器专属产物。

`wheel_smoke.py` 从独立源码目录构建 wheel，再 `--no-deps --target` 安装，使用 `-I`
隔离工作目录导入。断言 service、graph、graph_owner、graph_stages、attempt_policy
均来自安装目录，默认依赖元数据包含 `langgraph==1.2.17`，运行版本一致，再执行
case_001 / case_002 的正式服务、真实工具写入和真实 pytest。两例通过。

该检查复用当前解释器已有第三方依赖，不等于全新虚拟环境从网络安装全部依赖。
`pip check` 通过；Ruff lint 通过，format 检查 830 个文件通过。

未执行全量测试、真实模型、WSL/Docker、其他本地平台或远端 CI；没有性能、Token
消耗或修复率提升的测量结论。
