"""SandboxManager：Docker 容器生命周期管理。

一个容器 = 一个修复 Turn。每次 Verifier 运行创建新容器，执行完即销毁。

使用 tar 流式传输替代 bind mount，避免 Windows bind mount I/O 瓶颈。
"""

import io
import os
import tarfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path

BUILD_TIMEOUT_S = 600
TEST_TIMEOUT_S = 900


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class Sandbox:
    id: str
    profile: str
    timings: dict | None = None


class SandboxManager:
    """Docker 容器管理（封装 docker-py）。

    设计原则：
    - 一个容器 = 一次验证 Turn
    - tar 流式传文件进容器（绕过 Windows bind mount 性能问题）
    - 资源限制（mem_limit=4g, cpu_quota=200000）
    """

    IMAGE = "repair-agent/python-repair"

    def __init__(self):
        self._docker = None

    @property
    def docker(self):
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
        """
        import time

        repo = Path(repo_path).resolve()
        image = f"{self.IMAGE}" if profile == "python" else self.IMAGE
        timings: dict[str, int] = {}

        t0 = time.time()
        # entrypoint.sh 启动时会 cd /code；镜像内尚无 /code 时容器会立刻退出，
        # 导致 put_archive 报 RWLayer nil。保持容器存活用 bare sleep，exec 仍走 entrypoint。
        container = self.docker.containers.run(
            image,
            ["sleep", "infinity"],
            entrypoint="",
            mem_limit="4g",
            cpu_quota=200000,
            network_mode="none",
            detach=True,
            remove=True,
        )
        timings["container_create_ms"] = int((time.time() - t0) * 1000)

        t1 = time.time()
        self._copy_to_container(container, repo, "/code")
        timings["tar_copy_ms"] = int((time.time() - t1) * 1000)

        return Sandbox(id=container.id, profile=profile, timings=timings)

    def execute(self, sandbox: Sandbox, command: str, timeout: int = BUILD_TIMEOUT_S) -> ExecResult:
        """在容器内执行命令。

        Args:
            sandbox: Sandbox 实例。
            command: 要执行的命令。
            timeout: 超时秒数。

        Returns:
            ExecResult 实例。
        """
        container = self.docker.containers.get(sandbox.id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(container.exec_run, command)
            try:
                exit_code, output = fut.result(timeout=timeout)
            except FuturesTimeoutError:
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"timeout after {timeout}s",
                )
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

    def _copy_to_container(self, container, src: Path, dst: str):
        """用 put_archive 流式传文件进容器（替代 bind mount）。"""
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            # 只打包 Python 源码和配置（跳过 .git, __pycache__, .agent 等）
            for root, dirs, files in os.walk(src):
                # 跳过非必要目录
                dirs[:] = [
                    d
                    for d in dirs
                    if d
                    not in {
                        ".git",
                        "__pycache__",
                        ".pytest_cache",
                        ".agent",
                        ".venv",
                        "venv",
                        "node_modules",
                        ".ruff_cache",
                    }
                ]
                for name in files:
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    full = Path(root) / name
                    arcname = str(Path(dst) / full.relative_to(src)).replace("\\", "/")
                    tar.add(full, arcname=arcname)

        tar_stream.seek(0)
        container.put_archive("/", tar_stream)
