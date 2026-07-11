# Sandbox 逃逸回归 Case 设计

> **Bonus ref:** docs/bonus.md §16.5 — 逃逸回归 Case
> **Layer:** L2 (harness)
> **Status:** in progress

## FixLoop Context

- **Bonus ref:** docs/bonus.md §16.5
- **Layer:** L2 — `src/harness/sandbox_manager.py`
- **Primary modules:** `tests/test_sandbox_escape.py`（新建）
- **Acceptance:** `pytest tests/test_sandbox_escape.py -v`（需 Docker）
- **Branch:** V1.2-Bonus7-sandbox-tier

## 1. 目标

在 CI 可复现地验证 Docker sandbox 的五维隔离，确保后续变更不会意外削弱沙箱安全性。

## 2. 逃逸向量矩阵

| # | 向量 | 验证命令 | 预期结果 | 隔离维度 |
|---|------|------|------|:--:|
| 1 | 写保护 | `touch /etc/evil` | exit_code ≠ 0, "Read-only" in stderr | 文件系统 |
| 2 | 网络隔离 | `curl --connect-timeout 5 http://example.com` | exit_code ≠ 0 | 网络 |
| 3 | fork 炸弹 | `:(){ :|:& };:` | 容器被 kill / timeout | 资源 (cpu_quota) |
| 4 | 特权禁止 | `mount -t tmpfs tmpfs /tmp` | exit_code ≠ 0 | 权限 |
| 5 | 设备访问 | `dd if=/dev/sda of=/dev/null bs=1 count=1 2>&1` | exit_code ≠ 0 | 设备 |

## 3. 实现方式

### 3.1 测试结构

`tests/test_sandbox_escape.py` — 单文件，5 个测试方法，Docker 不可用时自动 skip。

```python
class TestSandboxEscape:
    """Docker sandbox 逃逸回归测试。每个测试创建独立容器。"""

    @pytest.fixture
    def sandbox(self) -> Sandbox:
        """真实 Docker 容器；Docker 不可用时 pytest.skip。"""
        ...

    def test_read_only_rootfs_blocks_write(self, sandbox): ...
    def test_network_none_blocks_egress(self, sandbox): ...
    def test_resource_limits_contain_fork_bomb(self, sandbox): ...
    def test_unprivileged_blocks_mount(self, sandbox): ...
    def test_device_access_is_blocked(self, sandbox): ...
```

### 3.2 安全原则

- 每个测试创建**独立容器**，执行完即 destroy
- 使用 `network_mode=none` + `read_only=True` + 非 privileged
- fork 炸弹测试加硬超时，防止拖死 CI
- 只验证容器内隔离，不尝试真实逃逸

## 4. 验收

- [ ] 5 个逃逸向量全部通过（Docker 可用时）
- [ ] Docker 不可用时全部 skip（含明确原因）
- [ ] `pytest tests/test_sandbox_escape.py -v` 可独立运行
- [ ] `docs/bonus/DESIGN.md` §16.5 补逃逸矩阵
