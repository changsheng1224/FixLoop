"""FIXLOOP_MAX_SANDBOXES Semaphore 并发上限单测。"""

import pytest


class TestSandboxSemaphore:
    def test_default_max_is_four(self, monkeypatch):
        """默认 FIXLOOP_MAX_SANDBOXES=4。"""
        monkeypatch.delenv("FIXLOOP_MAX_SANDBOXES", raising=False)
        import importlib

        import src.harness.sandbox_manager as sm

        importlib.reload(sm)
        assert sm._MAX_SANDBOXES == 4

    def test_env_override(self, monkeypatch):
        """FIXLOOP_MAX_SANDBOXES 可通过环境变量覆盖。"""
        monkeypatch.setenv("FIXLOOP_MAX_SANDBOXES", "2")
        import importlib

        import src.harness.sandbox_manager as sm

        importlib.reload(sm)
        assert sm._MAX_SANDBOXES == 2

    def test_semaphore_capacity_matches_max(self, monkeypatch):
        """Semaphore 容量等于 _MAX_SANDBOXES。"""
        monkeypatch.setenv("FIXLOOP_MAX_SANDBOXES", "3")
        import importlib

        import src.harness.sandbox_manager as sm

        importlib.reload(sm)
        assert sm._sandbox_semaphore._initial_value == 3

    def test_semaphore_is_bounded(self, monkeypatch):
        """BoundedSemaphore 防止 release() 多调。"""
        import threading

        sem = threading.BoundedSemaphore(1)
        assert sem.acquire(timeout=1)
        sem.release()
        with pytest.raises(ValueError):
            sem.release()

    def test_concurrent_limit_blocks_extra(self):
        """并发超过上限时 Semaphore 阻塞新请求。"""
        import threading

        sem = threading.BoundedSemaphore(1)
        assert sem.acquire(timeout=1)
        assert sem.acquire(timeout=0.1) is False
        sem.release()
        assert sem.acquire(timeout=1)
        sem.release()
