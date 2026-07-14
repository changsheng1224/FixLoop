"""SandboxManager：Docker 容器生命周期管理。

一个容器 = 一个修复 Turn。每次 Verifier 运行创建新容器，执行完即销毁。

使用 tar 流式传输替代 bind mount，避免 Windows bind mount I/O 瓶颈。
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.logging_setup import get_logger

log = get_logger(__name__)

# 模块级并发上限 Semaphore（env FIXLOOP_MAX_SANDBOXES，默认 4）
_MAX_SANDBOXES = int(os.getenv("FIXLOOP_MAX_SANDBOXES", "4"))
_sandbox_semaphore = threading.BoundedSemaphore(_MAX_SANDBOXES)

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
    """``containers.run`` 参数：网络/资源/只读 rootfs + 双 tmpfs。

    Security: privileged=False，不挂载 docker.sock（禁止 Docker-in-Docker）。
    """
    return {
        "image": image,
        "command": ["sleep", "infinity"],
        "entrypoint": "",
        "read_only": True,
        "privileged": False,
        "tmpfs": sandbox_tmpfs_mounts(),
        "mem_limit": "4g",
        "cpu_quota": 200000,
        "network_mode": "none",
        "detach": True,
        "remove": True,
    }


def assert_no_docker_sock(kwargs: dict) -> None:
    """验证容器参数不含 docker.sock 挂载（禁止 Docker-in-Docker）。

    Raises:
        ValueError: 若发现 docker.sock 挂载。
    """
    volumes = kwargs.get("volumes", {}) or {}
    binds = kwargs.get("binds", []) or []
    all_paths = list(volumes.keys()) + list(binds)
    for path in all_paths:
        if "docker.sock" in str(path):
            raise ValueError(f"docker.sock mount is forbidden: {path}")


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
    _semaphore_held: bool = False  # destroy 时是否需要 release semaphore


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

        受 FIXLOOP_MAX_SANDBOXES 控制并发上限（默认 4）。
        达到上限时阻塞等待，不抛异常。

        Args:
            repo_path: 仓库路径（tar 打包后传入 /code）。
            profile: 镜像 profile（默认 python）。

        Returns:
            Sandbox 实例。

        Raises:
            SandboxArchiveError: tar 排除后为空或超过大小上限。
        """
        import time

        acquired = _sandbox_semaphore.acquire(timeout=30)
        if not acquired:
            raise RuntimeError(
                f"FIXLOOP_MAX_SANDBOXES={_MAX_SANDBOXES}: "
                "无法在 30s 内获取 sandbox 槽位"
            )
        log.debug("sandbox semaphore acquired (%d/%d)",
                   _MAX_SANDBOXES - _sandbox_semaphore._value, _MAX_SANDBOXES)

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

        sb = Sandbox(id=container.id, profile=profile, timings=timings, _semaphore_held=acquired)
        return sb

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
        """销毁容器、释放 semaphore（若 create 时持有）、移除持久层。"""
        try:
            container = self.docker.containers.get(sandbox.id)
            container.kill()
            container.remove(force=True)
        except Exception:
            pass
        finally:
            if getattr(sandbox, "_semaphore_held", False):
                _sandbox_semaphore.release()
                log.debug("sandbox semaphore released (%d/%d)",
                           _MAX_SANDBOXES - _sandbox_semaphore._value, _MAX_SANDBOXES)


class SandboxContext:
    """Sandbox 上下文管理器：__exit__ 中 guaranteed destroy。

    Usage::

        with SandboxContext(mgr, sandbox) as sb:
            # sb is the sandbox; use for verify
            ...
        # sandbox destroyed + removed here
    """

    def __init__(self, mgr: SandboxManager, sandbox: Sandbox):
        self._mgr = mgr
        self._sandbox = sandbox

    def __enter__(self) -> Sandbox:
        return self._sandbox

    def __exit__(self, *exc) -> None:
        self._mgr.destroy(self._sandbox)
