"""Sandbox destroy + SandboxContext 单测（V1.4-Bonus14a）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.harness.sandbox_manager import SandboxContext


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
