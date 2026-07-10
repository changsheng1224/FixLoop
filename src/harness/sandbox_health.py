"""Sandbox 健康探针：docker info、镜像、network_mode=none 冒烟。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.harness.sandbox_manager import SandboxManager, sandbox_container_run_kwargs


@dataclass
class SandboxHealthReport:
    """结构化探针结果（``--health`` / repair_factory 共用）。"""

    ready: bool
    docker_ping: str
    docker_info: str
    image: str
    network_smoke: str
    image_ref: str = ""
    checks_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "docker_ping": self.docker_ping,
            "docker_info": self.docker_info,
            "image": self.image,
            "network_smoke": self.network_smoke,
            "image_ref": self.image_ref,
            "checks_ms": self.checks_ms,
            "errors": list(self.errors),
        }


def _docker_client(client=None):
    if client is not None:
        return client
    import docker

    return docker.from_env()


def _image_candidates(image: str) -> list[str]:
    if ":" in image:
        return [image]
    return [image, f"{image}:latest"]


def _resolve_image(client, image: str) -> tuple[str, str]:
    from docker.errors import ImageNotFound

    last_err = ""
    for ref in _image_candidates(image):
        try:
            client.images.get(ref)
            return ref, "ok"
        except ImageNotFound as exc:
            last_err = str(exc)
        except Exception as exc:
            return "", f"error:{exc}"
    return "", f"missing:{image}" + (f" ({last_err})" if last_err else "")


def _run_network_smoke(client, image_ref: str) -> str:
    kwargs = sandbox_container_run_kwargs(image_ref)
    kwargs["command"] = ["true"]
    kwargs["remove"] = True
    try:
        client.containers.run(**kwargs)
        return "ok"
    except Exception as exc:
        return f"error:{exc}"


def probe_sandbox_health(*, run_smoke: bool = True, client=None) -> SandboxHealthReport:
    """执行 sandbox 就绪检查；``ready=False`` 时 Verifier 应降级。"""
    t0 = time.time()
    errors: list[str] = []
    docker_ping = "error"
    docker_info = "skipped"
    image_status = "skipped"
    network_smoke = "skipped"
    image_ref = ""

    try:
        docker = _docker_client(client)
        docker.ping()
        docker_ping = "ok"
    except Exception as exc:
        errors.append(f"docker ping: {exc}")
        return SandboxHealthReport(
            ready=False,
            docker_ping=docker_ping,
            docker_info="skipped",
            image="skipped",
            network_smoke="skipped",
            checks_ms=int((time.time() - t0) * 1000),
            errors=errors,
        )

    try:
        info = docker.info()
        if not info.get("ServerVersion"):
            raise RuntimeError("docker info missing ServerVersion")
        docker_info = "ok"
    except Exception as exc:
        docker_info = f"error:{exc}"
        errors.append(f"docker info: {exc}")
        return SandboxHealthReport(
            ready=False,
            docker_ping=docker_ping,
            docker_info=docker_info,
            image="skipped",
            network_smoke="skipped",
            checks_ms=int((time.time() - t0) * 1000),
            errors=errors,
        )

    image_ref, image_status = _resolve_image(docker, SandboxManager.IMAGE)
    if image_status != "ok":
        errors.append(f"image: {image_status}")
        return SandboxHealthReport(
            ready=False,
            docker_ping=docker_ping,
            docker_info=docker_info,
            image=image_status,
            network_smoke="skipped",
            image_ref=image_ref,
            checks_ms=int((time.time() - t0) * 1000),
            errors=errors,
        )

    if run_smoke:
        network_smoke = _run_network_smoke(docker, image_ref)
        if network_smoke != "ok":
            errors.append(f"network_smoke: {network_smoke}")
            return SandboxHealthReport(
                ready=False,
                docker_ping=docker_ping,
                docker_info=docker_info,
                image=image_status,
                network_smoke=network_smoke,
                image_ref=image_ref,
                checks_ms=int((time.time() - t0) * 1000),
                errors=errors,
            )
    else:
        network_smoke = "skipped"

    return SandboxHealthReport(
        ready=True,
        docker_ping=docker_ping,
        docker_info=docker_info,
        image=image_status,
        network_smoke=network_smoke,
        image_ref=image_ref,
        checks_ms=int((time.time() - t0) * 1000),
        errors=errors,
    )
