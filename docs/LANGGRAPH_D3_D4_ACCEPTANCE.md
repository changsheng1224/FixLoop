# LangGraph 阶段图与正式接入（D3 / D4）

日期：2026-10-26。边界见 [D1](LANGGRAPH_ORCHESTRATION_BOUNDARY.md)，调用方式见
[D2](LANGGRAPH_EXECUTION_D2.md)。本文件记录本次实现与实际验证，不代替后续完整 D5 验收。

## D3：阶段图

- `src/repair/graph.py` 定义全部节点和条件边；图数据只有临时 `next_action`。
- `src/repair/graph_stages.py` 在 owner 上调用既有服务，维护原阶段计时与事件。
- `src/repair/attempt_policy.py` 提取原空补丁、Critic 和验证失败策略，不改变反馈、
  回滚、计数、止损和 checkpoint 的先后关系。
- `src/repair/graph_owner.py` 将 D2 已验证的调用桥移入生产模块；实验测试改为验证
  同一份实现，删除原测试专用副本。生产模块不导入 tests。
- `RepairState` 与 `ExecutionSession` 通过 runtime context 传入同一实例；不配置
  checkpointer、Store、缓存、节点自动重试或框架超时。
- 图步数保护按剩余尝试额度计算，覆盖大于框架默认步数的合法修复请求。

完整拓扑由实际编译图导出为 [LANGGRAPH_REPAIR_FLOW.mmd](LANGGRAPH_REPAIR_FLOW.mmd)。
可重新生成：

```powershell
python -c "from pathlib import Path; from src.repair.graph import repair_graph; Path('docs/LANGGRAPH_REPAIR_FLOW.mmd').write_text(repair_graph().get_graph().draw_mermaid(), encoding='utf-8')"
```

“验证失败 → 回滚并记录诊断反馈 → 重试或结束”是独立的 `verification_failure`
节点。扩展节点时增加对应阶段方法和 `STAGE_ROUTES` 连接即可；条件边只读路由。
涉及新业务判断、工具调用或持久化边界的扩展仍需单独评审，不允许绕过内核。

### D3 定向验证

同一工作区隔离解释器执行：

```powershell
.runtime-tools/langgraph-d2/Scripts/python.exe -m pytest tests/test_repair_graph.py tests/test_langgraph_execution.py -q -p no:cacheprovider --basetemp=.runtime-tools/langgraph-d2/pytest-d3
```

结果：22 passed。覆盖正常／恢复入口、原 State 与 owner 身份、Critic 拒绝、
明确空补丁终态和重试、接口保护先于验证开关、验证失败的回滚与当前账本反馈、
非测试结果、累计阶段超时、预取消，以及 300 次合法业务重试。
其中 D2 继承的 invoke／stream 测试实际执行文件修改、SQLite 和 pytest。

## D4：正式入口

待接入正式入口后补充实现与本次验证记录。D3 本身不切换公开修复路径。
