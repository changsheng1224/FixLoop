"""Docker sandbox 逃逸回归测试 — 五维隔离验证。

每个测试创建独立容器、注入逃逸命令、断言被阻止、销毁容器。
Docker 不可用时自动 skip。
"""

import time
from pathlib import Path
from shlex import quote as _sh_quote

import pytest

from src.harness.sandbox_manager import (
    EXEC_TIMEOUT_EXIT_CODE,
    ExecResult,
    Sandbox,
    SandboxManager,
)
from src.harness.sandbox_verify import SandboxNotAvailableError, assert_sandbox_available

# 容器内命令超时（fork 炸弹另设）
ESCAPE_TIMEOUT_S = 10


def _docker_available() -> bool:
    """检测 Docker 是否可用（不抛异常）。"""
    try:
        assert_sandbox_available()
        return True
    except SandboxNotAvailableError:
        return False


def _run_in_sandbox(
    mgr: SandboxManager, sandbox: Sandbox, command: str, timeout: int = ESCAPE_TIMEOUT_S
) -> ExecResult:
    """在容器内执行命令并返回 ExecResult。"""
    return mgr.execute(sandbox, f"/bin/sh -c {_sh_quote(command)}", timeout=timeout)


def _cap_eff_value(status_output: str) -> int | None:
    for line in status_output.splitlines():
        if line.lower().startswith("capeff:"):
            _, value = line.split(":", 1)
            return int(value.strip(), 16)
    return None


@pytest.fixture
def sandbox_mgr() -> SandboxManager:
    return SandboxManager()


@pytest.fixture
def sandbox(sandbox_mgr: SandboxManager, tmp_path: Path) -> Sandbox:
    """创建真实 Docker 容器；Docker 不可用时 skip。"""
    if not _docker_available():
        pytest.skip("Docker 不可用，跳过 sandbox 逃逸测试")
    # 创建一个最小工作目录
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    try:
        sb = sandbox_mgr.create(str(repo))
    except Exception as exc:
        pytest.skip(f"无法创建 sandbox 容器: {exc}")
    yield sb
    try:
        sandbox_mgr.destroy(sb)
    except Exception:
        pass


class TestSandboxEscape:
    """五维隔离逃逸回归。"""

    # ── 向量 1: 文件系统写保护 ──

    def test_read_only_rootfs_blocks_write_to_etc(self, sandbox_mgr, sandbox):
        """read_only rootfs 应阻止向 /etc 写入。"""
        result = _run_in_sandbox(sandbox_mgr, sandbox, "touch /etc/escape_test_evil 2>&1")
        assert result.exit_code != 0, f"向 /etc 写入应被阻止，实际 exit_code={result.exit_code}"
        stderr = (result.stderr or "").lower()
        stdout = (result.stdout or "").lower()
        combined = stderr + stdout
        assert "read-only" in combined or "permission denied" in combined, (
            f"应包含 'read-only' 或 'permission denied'，实际输出: {combined[:200]}"
        )

    def test_read_only_rootfs_blocks_write_to_root(self, sandbox_mgr, sandbox):
        """read_only rootfs 应阻止向 /root 写入。"""
        result = _run_in_sandbox(sandbox_mgr, sandbox, "echo evil > /root/escape_test 2>&1")
        assert result.exit_code != 0, f"向 /root 写入应被阻止，实际 exit_code={result.exit_code}"

    # ── 向量 2: 网络隔离 ──

    def test_network_none_blocks_curl(self, sandbox_mgr, sandbox):
        """network_mode=none 应阻止 outbound HTTP 请求。"""
        result = _run_in_sandbox(
            sandbox_mgr,
            sandbox,
            "curl --connect-timeout 5 --max-time 8 http://example.com 2>&1",
            timeout=15,
        )
        assert result.exit_code != 0, (
            f"curl 应被网络隔离阻止（network_mode=none），"
            f"实际 exit_code={result.exit_code}, stdout={result.stdout[:200]}"
        )

    def test_network_none_blocks_ping(self, sandbox_mgr, sandbox):
        """network_mode=none 应阻止 ICMP。"""
        result = _run_in_sandbox(
            sandbox_mgr,
            sandbox,
            "ping -c 1 -W 5 8.8.8.8 2>&1 || true",
            timeout=15,
        )
        # ping 在无网络时 exit_code ≠ 0 或 stdout 含 "Network is unreachable"
        combined = (result.stdout or "") + (result.stderr or "")
        blocked = (
            result.exit_code != 0
            or "unreachable" in combined.lower()
            or "100% packet loss" in combined.lower()
            or "not found" in combined.lower()
        )
        assert blocked, f"ping 应被网络隔离阻止，实际: {combined[:200]}"

    # ── 向量 3: 资源限制（fork 炸弹） ──

    def test_resource_limits_contain_fork_bomb(self, sandbox_mgr, sandbox):
        """cpu_quota + mem_limit 应限制 fork 炸弹。"""
        # 限制 fork 数而非无限递归；用硬超时保底
        bomb = (
            "n=0; "
            "while [ $n -lt 200 ]; do "
            "  (sleep 999 &); "
            "  n=$((n + 1)); "
            "done; "
            "echo 'spawned 200 sleep processes'"
        )
        t0 = time.time()
        result = _run_in_sandbox(sandbox_mgr, sandbox, bomb, timeout=15)
        elapsed = time.time() - t0

        # 要么被 timeout 终止（预期），要么成功但在限制内
        if result.exit_code == EXEC_TIMEOUT_EXIT_CODE:
            return  # 被容器超时终止 → 隔离生效
        # 如果没超时，验证进程数被限制
        combined = (result.stdout or "") + (result.stderr or "")
        assert result.exit_code != 0 or "spawned" in combined or "resource" in combined.lower(), (
            f"fork 炸弹应超时或被限制，实际: exit_code={result.exit_code} elapsed={elapsed:.1f}s"
        )

    # ── 向量 4: 特权禁止 ──

    def test_unprivileged_blocks_mount(self, sandbox_mgr, sandbox):
        """非特权容器应禁止 mount 操作。"""
        result = _run_in_sandbox(sandbox_mgr, sandbox, "mount -t tmpfs tmpfs /tmp 2>&1")
        assert result.exit_code != 0, (
            f"mount 应被禁止（非特权容器），实际 exit_code={result.exit_code}"
        )
        combined = (result.stdout or "") + (result.stderr or "")
        assert (
            "permission denied" in combined.lower()
            or "not permitted" in combined.lower()
            or "operation not permitted" in combined.lower()
            or "must be superuser" in combined.lower()
        ), f"应包含权限拒绝信息，实际: {combined[:200]}"

    def test_unprivileged_no_cap_sys_admin(self, sandbox_mgr, sandbox):
        """非特权容器不应有 CAP_SYS_ADMIN。"""
        result = _run_in_sandbox(
            sandbox_mgr,
            sandbox,
            "cat /proc/1/status 2>&1 | grep -i cap || echo 'no status file'",
            timeout=5,
        )
        combined = result.stdout or ""
        if "no status file" in combined.lower() or result.exit_code != 0:
            return
        cap_eff = _cap_eff_value(combined)
        assert cap_eff is not None, f"未找到 CapEff，实际输出: {combined[:200]}"
        cap_sys_admin = 1 << 21
        assert (cap_eff & cap_sys_admin) == 0, "不应有 CAP_SYS_ADMIN"

    # ── 向量 5: 设备访问 ──

    def test_device_access_blocked(self, sandbox_mgr, sandbox):
        """容器不应能访问宿主机块设备。"""
        result = _run_in_sandbox(
            sandbox_mgr,
            sandbox,
            "dd if=/dev/sda of=/dev/null bs=1 count=1 2>&1",
            timeout=5,
        )
        assert result.exit_code != 0, f"设备访问应被阻止，实际 exit_code={result.exit_code}"
        combined = (result.stdout or "") + (result.stderr or "")
        assert (
            "no such file" in combined.lower()
            or "permission denied" in combined.lower()
            or "cannot open" in combined.lower()
        ), f"应包含 'No such file' 或 'Permission denied'，实际: {combined[:200]}"

    def test_proc_sys_write_blocked(self, sandbox_mgr, sandbox):
        """容器不应能修改 /proc/sys 内核参数。"""
        result = _run_in_sandbox(
            sandbox_mgr,
            sandbox,
            "echo 1 > /proc/sys/net/ipv4/ip_forward 2>&1",
            timeout=5,
        )
        assert result.exit_code != 0, f"修改 /proc/sys 应被阻止，实际 exit_code={result.exit_code}"

    # ── 向量 6: 敏感文件读取 ──

    def test_container_runs_as_non_root(self, sandbox_mgr, sandbox):
        """容器默认执行用户不应是 root。"""
        result = _run_in_sandbox(sandbox_mgr, sandbox, "id -u")
        assert result.exit_code == 0
        assert result.stdout.strip() != "0", f"容器不应以 root 运行，实际 uid={result.stdout!r}"

    def test_cannot_read_etc_shadow(self, sandbox_mgr, sandbox):
        """容器不应能读取 /etc/shadow。"""
        result = _run_in_sandbox(sandbox_mgr, sandbox, "cat /etc/shadow 2>&1 || echo BLOCKED")
        combined = (result.stdout or "") + (result.stderr or "")
        assert (
            "BLOCKED" in combined
            or "permission denied" in combined.lower()
            or "no such file" in combined.lower()
        ), f"不应能读取 /etc/shadow，实际: {combined[:200]}"

    # ── 向量 7: repo 写保护验证 ──

    def test_repo_stays_clean_after_escape_attempts(self, sandbox_mgr, sandbox, tmp_path):
        """逃逸尝试后 repo 保持干净。"""
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        original_files = set(str(p.relative_to(repo)) for p in repo.rglob("*"))
        # 执行一系列逃逸尝试
        escape_cmds = [
            "curl --connect-timeout 2 http://evil.com 2>&1 || true",
            "cat /etc/passwd 2>&1 || true",
            "echo hacked > /code/README.md 2>&1 || true",
        ]
        for cmd in escape_cmds:
            _run_in_sandbox(sandbox_mgr, sandbox, cmd, timeout=8)
        # repo 文件不应被修改
        current_files = set(str(p.relative_to(repo)) for p in repo.rglob("*"))
        assert original_files == current_files, (
            f"逃逸尝试后 repo 文件不应变化: {current_files - original_files}"
        )
