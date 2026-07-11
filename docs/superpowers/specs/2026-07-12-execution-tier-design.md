# execution_tier 工具执行分层设计

> **Bonus ref:** docs/bonus.md §16.7 — run_shell 宿主机 vs sandbox_verify 容器
> **Layer:** L1 + L2 both
> **Status:** draft → plan

## FixLoop Context

- **Bonus ref:** docs/bonus.md §16.7
- **Layer:** L1（工具注册 + trace）+ L2（sandbox 体检）
- **Primary modules:** `agent_runtime/tools.py`, `agent_runtime/tool_rejection.py`, `agent_runtime/agent_loop.py`, `src/harness/sandbox_verify.py`, `src/repair/verify.py`
- **Acceptance:** `pytest tests/test_tool_executor.py tests/test_sandbox_tools.py tests/test_loop_trace_snapshot.py -v`
- **Branch:** V1.2-Bonus7-sandbox-tier

## 1. 目标

为每个工具调用标注 `execution_tier: "host" | "container"`，实现：

1. **可观测**：trace.jsonl 的 `tool_executed` 事件 + report.json 汇总
2. **沙箱路径体检**：container tier 工具启动时断言必经 `SandboxManager.execute()`；Docker 不可用降级到 host 时标记 `execution_tier=host` + emit warn
3. **Threat model 文档**：明确 L1 宿主机 vs L2 Docker 的安全边界

## 2. 设计

### 2.1 execution_tier 声明

工具注册字典新增可选字段 `"execution_tier"`：

```python
# agent_runtime/tools.py — L1 工具默认 host
"run_shell": {
    "schema": {...},
    "risky": True,
    "execution_tier": "host",  # 显式声明
    ...
}

# src/tools/sandbox_tools.py — L2 sandbox 工具标记 container
"sandbox_verify": {
    "schema": {...},
    "risky": False,
    "execution_tier": "container",
    ...
}
```

- 默认值：`"host"`（未声明时）
- L1 所有工具 = `host`
- L2 `sandbox_build` / `sandbox_test` / `sandbox_verify` = `container`

### 2.2 数据流

```
工具注册 (execution_tier)
  └─→ ToolExecutor.execute_gated()
        └─→ ToolExecutionResult.metadata["execution_tier"]
              ├─→ trace.jsonl: tool_executed {..., execution_tier}
              └─→ report.json: tier_summary {host_calls, container_calls, host_tools: {...}}
```

**关键修改点：**

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1. 声明 | `agent_runtime/tools.py` | `BASE_TOOLS` 每项加 `execution_tier` |
| 2. 声明 | `src/tools/sandbox_tools.py` | `build_sandbox_tool_registry()` 每项加 `execution_tier: "container"` |
| 3. 注入 | `agent_runtime/tool_executor.py` | Gate 9 成功后从 `tool_spec` 读取 `execution_tier` 写入 `metadata` |
| 4. Trace | `agent_runtime/tool_rejection.py` | `TOOL_TRACE_PUBLIC_KEYS` 加 `"execution_tier"` |
| 5. Report | `agent_runtime/run_store.py` | `append_trace` 聚合 tier 计数进 report |
| 6. 体检 | `src/harness/sandbox_verify.py` | container tier 工具入口断言 `SandboxManager` 可用 |
| 7. 降级 | `src/repair/verify.py` | `PytestVerifyStrategy` 标记 `execution_tier=host` + warn |

### 2.3 沙箱路径体检

`run_sandbox_verification_flow()` 开头新增轻量体检：

```python
def _assert_sandbox_available():
    """container tier 工具的前置检查。"""
    try:
        import docker
        docker.from_env().ping()
    except Exception:
        raise SandboxNotAvailableError("Docker 不可用，container tier 工具无法执行")
```

- Docker 不可用 → 抛 `SandboxNotAvailableError`
- Orchestrator catch → 降级到 `PytestVerifyStrategy`（已有逻辑），trace 标记 `execution_tier=host`
- 降级时 emit warn 日志 + `agent_errors["verifier"] = "sandbox_unavailable_degraded_to_host"`

### 2.4 Threat Model（概要）

| 层级 | 工具 | 隔离 | 风险 |
|------|------|------|------|
| **host** | `run_shell`, `write_file`, `patch_file` | Gate+quota+approval | 可触及宿主机文件系统、网络 |
| **container** | `sandbox_verify` | Docker `network=none` + `read_only` rootfs + tmpfs `/code` `/tmp` | 容器逃逸（已知 Docker 非绝对安全）；不防逻辑错误 |

**已知限制**：
- `network=none` 意味着补丁代码无法 `pip install` 运行时依赖（需预装镜像）
- Docker 不防逻辑错误（错误 patch 仍可能通过 pytest）
- 不声称"绝对安全" — Docker 逃逸是已知攻击面

## 3. report.json 聚合格式

```json
{
  "tier_summary": {
    "host_calls": 12,
    "container_calls": 2,
    "host_tools": {"read_file": 5, "search": 3, "write_file": 2, "run_shell": 2},
    "container_tools": {"sandbox_verify": 2}
  }
}
```

## 4. 验收

- [ ] `TOOL_TRACE_PUBLIC_KEYS` 含 `execution_tier`
- [ ] `tool_executed` trace 事件包含 `execution_tier` 字段
- [ ] `report.json` 含 `tier_summary`
- [ ] `container` tier 工具在 Docker 不可用时抛明确异常
- [ ] 降级路径 trace 标记 `execution_tier=host`
- [ ] 现有 Gateway 权限不受影响

## 5. 测试

- `tests/test_tool_executor.py`：验证 `execution_tier` 正确注入 metadata
- `tests/test_loop_trace_snapshot.py`：验证 trace 含 `execution_tier`
- `tests/test_sandbox_tools.py`：验证 sandbox 工具声明 `container`
- `tests/test_verify.py`（新增或扩）：验证 Docker 不可用时降级 + warn
