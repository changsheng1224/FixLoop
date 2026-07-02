"""SandboxManager：Docker 容器生命周期管理。

一个容器 = 一个修复 Turn。每次 Verifier 运行创建新容器，执行完即销毁。
"""

from dataclasses import dataclass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class Sandbox:
    id: str
    profile: str


class SandboxManager:
    """Docker 容器管理（封装 docker-py）。

    设计原则：
    - 一个容器 = 一次验证 Turn
    - 仓库只读挂载（mode="ro"），宿主机零副作用
    - 网络隔离（network_mode="none"）
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
        """创建隔离容器。

        Args:
            repo_path: 仓库路径（只读挂载到 /code）。
            profile: 镜像 profile（默认 python）。

        Returns:
            Sandbox 实例。
        """
        image = f"{self.IMAGE}" if profile == "python" else self.IMAGE
        container = self.docker.containers.run(
            image,
            "tail -f /dev/null",
            volumes={repo_path: {"bind": "/code", "mode": "ro"}},
            network_mode="none",
            mem_limit="4g",
            cpu_quota=200000,
            detach=True,
            remove=True,
        )
        return Sandbox(id=container.id, profile=profile)

    def execute(self, sandbox: Sandbox, command: str, timeout: int = 600) -> ExecResult:
        """在容器内执行命令。

        Args:
            sandbox: Sandbox 实例。
            command: 要执行的命令。
            timeout: 超时秒数。

        Returns:
            ExecResult 实例。
        """
        container = self.docker.containers.get(sandbox.id)
        exit_code, output = container.exec_run(command, timeout=timeout)
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
