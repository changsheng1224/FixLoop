"""SandboxManager：Docker 容器生命周期管理。

一个容器 = 一个修复 Turn。每次 Verifier 运行创建新容器，执行完即销毁。

使用 tar 流式传输替代 bind mount，避免 Windows bind mount I/O 瓶颈。
"""

import base64
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.cancellation import (
    BlockingDeadlineError,
    CancelledError,
    wait_future,
)
from agent_runtime.logging_setup import get_logger
from src.harness.sandbox_tar import build_sandbox_tar

log = get_logger(__name__)

# 模块级并发上限 Semaphore（env FIXLOOP_MAX_SANDBOXES，默认 4）
_MAX_SANDBOXES = int(os.getenv("FIXLOOP_MAX_SANDBOXES", "4"))
_sandbox_semaphore = threading.BoundedSemaphore(_MAX_SANDBOXES)

BUILD_TIMEOUT_S = 600
TEST_TIMEOUT_S = 900
EXEC_TIMEOUT_EXIT_CODE = -1
EXEC_USER_CANCEL_EXIT_CODE = -2

# 可写面仅 /code + /tmp（read_only rootfs + tmpfs）；大小可通过环境变量调节。
# mode=1777 必需：仅 size=… 时部分 Docker 会挂成 0755 root，nobody 无法 tar 解包到 /code
# →「sandbox upload did not complete」。
DEFAULT_TMPFS_TMP = "size=512m,mode=1777"
DEFAULT_TMPFS_CODE = "size=2g,mode=1777"
DEFAULT_SANDBOX_USER = "65534:65534"
SANDBOX_HOME = "/tmp/fixloop-home"
SANDBOX_PYTHONUSERBASE = "/tmp/fixloop-userbase"
SANDBOX_UPLOAD_DONE_MARKER = "/tmp/fixloop_upload_done"
TTY_EOF = b"\x04"
SANDBOX_TAR_UPLOAD_COMMAND = [
    "/bin/sh",
    "-c",
    "stty -echo 2>/dev/null || true; "
    f"base64 -d | tar -C /code -xf - && touch {SANDBOX_UPLOAD_DONE_MARKER}",
]


def sandbox_tmpfs_mounts() -> dict[str, str]:
    """read_only 容器下的可写 tmpfs 挂载。

    保证含 ``mode=1777``：部分 Docker 对仅 ``size=…`` 的 tmpfs 挂成 0755，
    导致非 root 用户无法写入 /code。
    """

    def _ensure_world_writable(opts: str) -> str:
        raw = (opts or "").strip()
        if "mode=" in raw:
            return raw
        return f"{raw},mode=1777" if raw else "mode=1777"

    return {
        "/tmp": _ensure_world_writable(
            os.getenv("FIXLOOP_SANDBOX_TMPFS_TMP", DEFAULT_TMPFS_TMP)
        ),
        "/code": _ensure_world_writable(
            os.getenv("FIXLOOP_SANDBOX_TMPFS_CODE", DEFAULT_TMPFS_CODE)
        ),
    }


def sandbox_container_run_kwargs(image: str) -> dict:
    """``containers.run`` 参数：网络/资源/只读 rootfs + 双 tmpfs。

    Security: 非 root、cap_drop=ALL、no-new-privileges，不挂载 docker.sock。
    """
    return {
        "image": image,
        "command": ["sleep", "infinity"],
        "entrypoint": "",
        "user": os.getenv("FIXLOOP_SANDBOX_USER", DEFAULT_SANDBOX_USER),
        "read_only": True,
        "privileged": False,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "tmpfs": sandbox_tmpfs_mounts(),
        "environment": {
            "HOME": SANDBOX_HOME,
            "PYTHONUSERBASE": SANDBOX_PYTHONUSERBASE,
        },
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


def sandbox_shell_argv(command: str) -> list[str]:
    """将 shell 字符串交给 ``/bin/sh -c``，避免 docker-py 对含 ``&&``/``|`` 的串做 shlex 拆分。

    根因（E16）：``exec_run("mkdir ... && ... pip install --user ...")`` 会被拆成 argv，
    BusyBox ``mkdir`` 收到 ``--user`` → ``unrecognized option '--user'``。
    """
    return ["/bin/sh", "-c", command]


def sandbox_pythonpath_prefix(repo_path: str | Path | None = None) -> str:
    """为源码布局设置 PYTHONPATH（离线时 pip -e 失败仍可 import 包）。

    始终包含 ``/code``；若仓库存在 ``lib/`` / ``src/``（或未传 repo）再追加对应路径。
    matplotlib 用 ``lib/``，src-layout 用 ``src/``。
    """
    parts = ["/code"]
    include_lib = True
    include_src = True
    if repo_path is not None:
        root = Path(repo_path)
        include_lib = (root / "lib").is_dir()
        include_src = (root / "src").is_dir()
    if include_lib:
        parts.append("/code/lib")
    if include_src:
        parts.append("/code/src")
    joined = ":".join(parts)
    return f'export PYTHONPATH="{joined}${{PYTHONPATH:+:$PYTHONPATH}}"'


def _sanitize_pip_package_names(packages: list[str] | None) -> list[str]:
    """仅保留安全的包名 token，避免注入 shell。"""
    out: list[str] = []
    for raw in packages or []:
        name = str(raw).strip()
        if name and all(c.isalnum() or c in "_.-+" for c in name):
            out.append(name)
        if len(out) >= 24:
            break
    return out


def sandbox_pip_install_command(
    *,
    extra_packages: list[str] | None = None,
    repo_path: str | Path | None = None,
) -> str:
    """pip 写入限定在 /tmp userbase；保留真实 exit code（不用 ``| tail`` 吞掉）。

    根因：``pip ... 2>&1 | tail -20`` 在无 pipefail 时 exit 恒为 0，
    导致 django/matplotlib 安装失败仍继续 pytest → ModuleNotFoundError。
    """
    path_export = sandbox_pythonpath_prefix(repo_path)
    pkgs = _sanitize_pip_package_names(extra_packages)
    extra_step = ""
    if pkgs:
        quoted = " ".join(pkgs)
        # 额外依赖尽力安装，不覆盖 -e 的 exit code
        extra_step = (
            f'/entrypoint.sh build python -m pip install --user {quoted} '
            f"> /tmp/pip_extra.txt 2>&1 || true; "
            f"tail -20 /tmp/pip_extra.txt; "
        )
    return (
        f"mkdir -p {SANDBOX_HOME} {SANDBOX_PYTHONUSERBASE} && "
        f"{path_export} && "
        "/entrypoint.sh build python -m pip install --user -e /code "
        "> /tmp/pip_editable.txt 2>&1; "
        "ec=$?; "
        "tail -20 /tmp/pip_editable.txt; "
        f"{extra_step}"
        "exit $ec"
    )


class _SocketWriter:
    """将 file-like write() 桥接到 Docker exec socket.sendall()."""

    def __init__(self, sock):
        self._sock = sock

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)


def _exec_output_socket(result):
    if isinstance(result, tuple):
        return result[1]
    return getattr(result, "output", result)


def _exec_exit_code(result) -> int:
    if isinstance(result, tuple):
        return result[0] or 0
    return getattr(result, "exit_code", 0) or 0


def _drain_exec_socket(sock) -> None:
    try:
        sock.settimeout(10)
    except Exception:
        pass
    while True:
        try:
            chunk = sock.recv(4096)
        except TimeoutError as exc:
            raise RuntimeError("sandbox upload did not finish before socket timeout") from exc
        except Exception:
            break
        if not chunk:
            break


def _close_exec_socket(sock) -> None:
    try:
        sock.close()
    except Exception:
        pass


def _copy_tar_to_code_tmpfs(container, tar_stream) -> None:
    """通过 exec stdin 将 tar 解包到 /code tmpfs。

    Docker put_archive 会在 read_only rootfs 容器上拒绝写入，即便目标路径是
    tmpfs；这里改为容器内 tar 从 stdin 解包，保持 rootfs 只读不变。
    """
    result = container.exec_run(
        SANDBOX_TAR_UPLOAD_COMMAND,
        stdin=True,
        socket=True,
        tty=True,
    )
    sock = _exec_output_socket(result)
    try:
        base64.encode(tar_stream, _SocketWriter(sock))
        sock.sendall(TTY_EOF)
        _drain_exec_socket(sock)
    finally:
        _close_exec_socket(sock)

    deadline = time.time() + 10
    while time.time() < deadline:
        marker = container.exec_run(["test", "-f", SANDBOX_UPLOAD_DONE_MARKER])
        if _exec_exit_code(marker) == 0:
            return
        time.sleep(0.05)
    raise RuntimeError("sandbox upload did not complete")


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

        repo = Path(repo_path).resolve()
        image = self.IMAGE
        timings: dict[str, int] = {}

        t_pack = time.time()
        tar_stream, stats = build_sandbox_tar(repo, ".")
        timings["tar_pack_ms"] = int((time.time() - t_pack) * 1000)
        timings["tar_bytes"] = stats.total_bytes
        timings["tar_file_count"] = stats.file_count
        timings["tar_max_bytes"] = stats.max_bytes

        acquired = _sandbox_semaphore.acquire(timeout=30)
        if not acquired:
            raise RuntimeError(
                f"FIXLOOP_MAX_SANDBOXES={_MAX_SANDBOXES}: 无法在 30s 内获取 sandbox 槽位"
            )
        log.debug(
            "sandbox semaphore acquired (%d/%d)",
            _MAX_SANDBOXES - _sandbox_semaphore._value,
            _MAX_SANDBOXES,
        )

        container = None
        t0 = time.time()
        try:
            # entrypoint.sh 启动时会 cd /code；镜像内尚无 /code 时容器会立刻退出，
            # 导致 put_archive 报 RWLayer nil。保持容器存活用 bare sleep，exec 仍走 entrypoint。
            container = self.docker.containers.run(**sandbox_container_run_kwargs(image))
            timings["container_create_ms"] = int((time.time() - t0) * 1000)

            t1 = time.time()
            tar_stream.seek(0)
            _copy_tar_to_code_tmpfs(container, tar_stream)
            timings["tar_copy_ms"] = int((time.time() - t1) * 1000)
        except Exception:
            if container is not None:
                try:
                    container.kill()
                except Exception:
                    pass
            _sandbox_semaphore.release()
            raise

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
        # 必须走 shell：pip/pytest 命令含 &&、管道与重定向（见 sandbox_shell_argv）。
        fut = pool.submit(container.exec_run, sandbox_shell_argv(command))
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
                log.debug(
                    "sandbox semaphore released (%d/%d)",
                    _MAX_SANDBOXES - _sandbox_semaphore._value,
                    _MAX_SANDBOXES,
                )


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
