"""SandboxManager：Docker 容器生命周期管理。

一个容器 = 一个修复 Turn。每次 Verifier 运行创建新容器，执行完即销毁。

使用 tar 流式传输替代 bind mount，避免 Windows bind mount I/O 瓶颈。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.cancellation import (
    BlockingDeadlineError,
    CancelledError,
    wait_future,
)
from src.harness.sandbox_tar import build_sandbox_tar

BUILD_TIMEOUT_S = 600
TEST_TIMEOUT_S = 900
EXEC_TIMEOUT_EXIT_CODE = -1
EXEC_USER_CANCEL_EXIT_CODE = -2

# 可写面仅 /code + /tmp（read_only rootfs + tmpfs）；大小可通过环境变量调节。
DEFAULT_TMPFS_TMP = "size=512m"
DEFAULT_TMPFS_CODE = "size=2g"


def sandbox_tmpfs_mounts() -> dict[str, str]:
    """read_only 容器下的可写 tmpfs 挂载。"""
    return {
        "/tmp": os.getenv("FIXLOOP_SANDBOX_TMPFS_TMP", DEFAULT_TMPFS_TMP),
        "/code": os.getenv("FIXLOOP_SANDBOX_TMPFS_CODE", DEFAULT_TMPFS_CODE),
    }


def sandbox_container_run_kwargs(image: str) -> dict:
    """``containers.run`` 参数：网络/资源/只读 rootfs + 双 tmpfs。"""
    return {
        "image": image,
        "command": ["sleep", "infinity"],
        "entrypoint": "",
        "read_only": True,
        "tmpfs": sandbox_tmpfs_mounts(),
        "mem_limit": "4g",
        "cpu_quota": 200000,
        "network_mode": "none",
        "detach": True,
        "remove": True,
    }


def sandbox_pip_install_command() -> str:
    """pip 写入限定在 /code/.local（只读 rootfs 下不可写 /usr/local）。"""
    return "/entrypoint.sh build pip install --user -e /code 2>&1 | tail -5"


@dataclass
class ExecResult:
    """容器内命令执行结果。"""

    exit_code: int
    stdout: str
    stderr: str
    cancelled: bool = False


@dataclass
class Sandbox:
    """活跃容器句柄（含可选耗时统计）。"""

    id: str
    profile: str
    timings: dict | None = None


class SandboxManager:
    """Docker 容器管理（封装 docker-py）。

    设计原则：
    - 一个容器 = 一次验证 Turn
    - tar 预检打包（排除 + 大小上限）后再创建容器
    - tar 流式传文件进容器（绕过 Windows bind mount 性能问题）
    - read_only rootfs + tmpfs /code、/tmp 为唯一可写面
    - 资源限制（mem_limit=4g, cpu_quota=200000）
    """

    IMAGE = "repair-agent/python-repair"

    def __init__(self):
        self._docker = None

    @property
    def docker(self):
        """懒加载 docker-py 客户端（首次调用时 ping）。"""
        if self._docker is None:
            import docker

            self._docker = docker.from_env()
        return self._docker

    def create(self, repo_path: str, profile: str = "python") -> Sandbox:
        """创建隔离容器并通过 tar 流式传文件。

        Args:
            repo_path: 仓库路径（tar 打包后传入 /code）。
            profile: 镜像 profile（默认 python）。

        Returns:
            Sandbox 实例。

        Raises:
            SandboxArchiveError: tar 排除后为空或超过大小上限。
        """
        import time

        repo = Path(repo_path).resolve()
        image = self.IMAGE
        timings: dict[str, int] = {}

        t_pack = time.time()
        tar_stream, stats = build_sandbox_tar(repo, "/code")
        timings["tar_pack_ms"] = int((time.time() - t_pack) * 1000)
        timings["tar_bytes"] = stats.total_bytes
        timings["tar_file_count"] = stats.file_count
        timings["tar_max_bytes"] = stats.max_bytes

        t0 = time.time()
        # entrypoint.sh 启动时会 cd /code；镜像内尚无 /code 时容器会立刻退出，
        # 导致 put_archive 报 RWLayer nil。保持容器存活用 bare sleep，exec 仍走 entrypoint。
        container = self.docker.containers.run(**sandbox_container_run_kwargs(image))
        timings["container_create_ms"] = int((time.time() - t0) * 1000)

        t1 = time.time()
        try:
            tar_stream.seek(0)
            container.put_archive("/", tar_stream)
        except Exception:
            try:
                container.kill()
            except Exception:
                pass
            raise
        timings["tar_copy_ms"] = int((time.time() - t1) * 1000)

        return Sandbox(id=container.id, profile=profile, timings=timings)

    def execute(
        self,
        sandbox: Sandbox,
        command: str,
        timeout: int = BUILD_TIMEOUT_S,
        cancel_token=None,
    ) -> ExecResult:
        """在容器内执行命令。

        Args:
            sandbox: Sandbox 实例。
            command: 要执行的命令。
            timeout: 超时秒数。
            cancel_token: 可选；置位时 SIGTERM 容器并返回 cancel 结果。

        Returns:
            ExecResult 实例。
        """
        poll_s = 0.05
        container = self.docker.containers.get(sandbox.id)

        def _kill_container() -> None:
            try:
                container.kill()
            except Exception:
                pass

        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(container.exec_run, command)
        deadline = time.time() + timeout if timeout > 0 else None
        try:
            try:
                exit_code, output = wait_future(
                    fut,
                    poll_interval=poll_s,
                    cancel_token=cancel_token,
                    deadline=deadline,
                    on_cancel=_kill_container,
                )
            except CancelledError:
                return ExecResult(
                    exit_code=EXEC_USER_CANCEL_EXIT_CODE,
                    stdout="",
                    stderr="",
                    cancelled=True,
                )
            except BlockingDeadlineError:
                return ExecResult(
                    exit_code=EXEC_TIMEOUT_EXIT_CODE,
                    stdout="",
                    stderr=f"timeout after {timeout}s",
                )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return ExecResult(
            exit_code=exit_code or 0,
            stdout=output.decode("utf-8", errors="replace") if output else "",
            stderr="",
        )

    def destroy(self, sandbox: Sandbox):
        """销毁容器。"""
        try:
            container = self.docker.containers.get(sandbox.id)
            container.kill()
        except Exception:
            pass
