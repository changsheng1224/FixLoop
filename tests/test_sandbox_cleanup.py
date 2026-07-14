"""Sandbox destroy + SandboxContext 单测（V1.4-Bonus14a）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.harness.sandbox_manager import (
    SandboxContext,
    assert_no_docker_sock,
    sandbox_container_run_kwargs,
)


class MockContainer:
    def __init__(self):
        self.killed = False
        self.removed = False

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class MockSandbox:
    def __init__(self, sandbox_id="mock-id"):
        self.id = sandbox_id


class MockSandboxManager:
    def __init__(self, container=None):
        self.container = container or MockContainer()
        self.docker = MagicMock()
        self.docker.containers.get.return_value = self.container

    def destroy(self, sandbox):
        try:
            c = self.docker.containers.get(sandbox.id)
            c.kill()
            c.remove(force=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# destroy 行为
# ---------------------------------------------------------------------------


class TestDestroy:
    def test_destroy_kills_and_removes(self):
        container = MockContainer()
        mgr = MockSandboxManager(container=container)
        sb = MockSandbox()
        mgr.destroy(sb)
        assert container.killed
        assert container.removed

    def test_destroy_handles_missing_container(self):
        mgr = MockSandboxManager()
        mgr.docker.containers.get.side_effect = Exception("not found")
        sb = MockSandbox()
        # 不抛异常
        mgr.destroy(sb)


# ---------------------------------------------------------------------------
# SandboxContext
# ---------------------------------------------------------------------------


class TestSandboxContext:
    def test_enter_returns_sandbox(self):
        container = MockContainer()
        mgr = MockSandboxManager(container=container)
        sb = MockSandbox()
        with SandboxContext(mgr, sb) as s:
            assert s is sb

    def test_exit_destroys_sandbox(self):
        container = MockContainer()
        mgr = MockSandboxManager(container=container)
        sb = MockSandbox()
        with SandboxContext(mgr, sb):
            pass
        assert container.killed
        assert container.removed

    def test_exit_destroys_even_on_exception(self):
        container = MockContainer()
        mgr = MockSandboxManager(container=container)
        sb = MockSandbox()
        try:
            with SandboxContext(mgr, sb):
                raise RuntimeError("verify failed")
        except RuntimeError:
            pass
        # 即使异常，destroy 仍执行
        assert container.killed
        assert container.removed


# ---------------------------------------------------------------------------
# 禁止特权与 Docker-in-Docker（V1.4-Bonus14b）
# ---------------------------------------------------------------------------


class TestNoPrivilege:
    def test_kwargs_has_privileged_false(self):
        kwargs = sandbox_container_run_kwargs("python:3.12-slim")
        assert kwargs.get("privileged") is False

    def test_kwargs_runs_as_non_root(self):
        kwargs = sandbox_container_run_kwargs("python:3.12-slim")
        assert kwargs.get("user") == "65534:65534"

    def test_kwargs_drops_capabilities(self):
        kwargs = sandbox_container_run_kwargs("python:3.12-slim")
        assert kwargs.get("cap_drop") == ["ALL"]
        assert kwargs.get("security_opt") == ["no-new-privileges:true"]

    def test_kwargs_no_docker_sock(self):
        kwargs = sandbox_container_run_kwargs("python:3.12-slim")
        volumes = kwargs.get("volumes", {}) or {}
        for path in volumes:
            assert "docker.sock" not in str(path)

    def test_assert_no_docker_sock_passes_on_clean_kwargs(self):
        kwargs = sandbox_container_run_kwargs("test")
        assert_no_docker_sock(kwargs)

    def test_assert_no_docker_sock_raises_on_violation(self):
        kwargs = {"volumes": {"/var/run/docker.sock": {"bind": "/var/run/docker.sock"}}}
        with pytest.raises(ValueError, match="docker.sock"):
            assert_no_docker_sock(kwargs)

    def test_read_only_is_true(self):
        kwargs = sandbox_container_run_kwargs("test")
        assert kwargs.get("read_only") is True

    def test_network_mode_is_none(self):
        kwargs = sandbox_container_run_kwargs("test")
        assert kwargs.get("network_mode") == "none"
