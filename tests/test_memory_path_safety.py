"""Memory 路径隔离单测：resolve_memory_path + MemoryPathError。"""

import tempfile
from pathlib import Path

import pytest

from agent_runtime.features.memory.candidate import (
    MemoryPathError,
    resolve_memory_path,
)


@pytest.fixture
def memory_root():
    """创建临时 .agent/memory 目录作为 memory root。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / ".agent" / "memory"
        root.mkdir(parents=True)
        (root / "topics").mkdir()
        yield str(root)


class TestResolveMemoryPath:
    def test_relative_path_returns_absolute(self, memory_root):
        result = resolve_memory_path(memory_root, "topics/test.md")
        assert result.is_absolute()
        assert str(result).endswith("test.md")

    def test_normal_path_within_root(self, memory_root):
        result = resolve_memory_path(memory_root, "topics/project-conventions.md")
        assert "topics" in str(result)
        assert "project-conventions.md" in str(result)

    def test_empty_path_raises(self, memory_root):
        with pytest.raises(MemoryPathError):
            resolve_memory_path(memory_root, "")

    def test_dotdot_traversal_rejected(self, memory_root):
        with pytest.raises(MemoryPathError, match="越界"):
            resolve_memory_path(memory_root, "../etc/passwd")

    def test_double_dotdot_traversal_rejected(self, memory_root):
        with pytest.raises(MemoryPathError):
            resolve_memory_path(memory_root, "../../../../etc/passwd")

    def test_absolute_path_outside_root_rejected(self, memory_root):
        with pytest.raises(MemoryPathError):
            resolve_memory_path(memory_root, "/etc/passwd")

    def test_absolute_path_inside_root_allowed(self, memory_root):
        abs_path = str(Path(memory_root) / "topics" / "test.md")
        result = resolve_memory_path(memory_root, abs_path)
        assert result == Path(abs_path).resolve()

    def test_dot_path_resolves(self, memory_root):
        result = resolve_memory_path(memory_root, "./topics/test.md")
        assert result.is_absolute()
        assert "topics" in str(result)

    def test_memory_path_error_contains_raw_path(self, memory_root):
        with pytest.raises(MemoryPathError) as exc:
            resolve_memory_path(memory_root, "../secret")
        assert "../secret" in str(exc.value.raw_path)

    def test_memory_path_error_is_value_error_subclass(self):
        """MemoryPathError 是 ValueError 子类，兼容既有 except ValueError。"""
        assert issubclass(MemoryPathError, ValueError)


class TestDurableEnsureWithin:
    def test_topics_dir_path_is_safe(self, tmp_path):
        """正常 topic 文件路径通过 _ensure_within 检查。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        memory_root = tmp_path / ".agent" / "memory"
        memory_root.mkdir(parents=True)
        (memory_root / "topics").mkdir()

        store = DurableMemoryStore(str(tmp_path))
        # 不会抛异常
        store._ensure_within(store.topics_dir / "project-conventions.md")

    def test_dotdot_rejected_by_ensure_within(self, tmp_path):
        """含 .. 的路径被 _ensure_within 拒绝。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        memory_root = tmp_path / ".agent" / "memory"
        memory_root.mkdir(parents=True)
        (memory_root / "topics").mkdir()

        store = DurableMemoryStore(str(tmp_path))
        with pytest.raises((MemoryPathError, ValueError)):
            store._ensure_within(store.topics_dir / ".." / "secret.md")

    def test_absolute_outside_rejected_by_ensure_within(self, tmp_path):
        """workspace 外绝对路径被 _ensure_within 拒绝。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        memory_root = tmp_path / ".agent" / "memory"
        memory_root.mkdir(parents=True)
        (memory_root / "topics").mkdir()

        store = DurableMemoryStore(str(tmp_path))
        with pytest.raises((MemoryPathError, ValueError)):
            store._ensure_within(Path("/etc/passwd"))


class TestResolveMemoryPathImport:
    def test_importable_from_package(self):
        from agent_runtime.features.memory import MemoryPathError, resolve_memory_path

        assert callable(resolve_memory_path)
        assert issubclass(MemoryPathError, ValueError)
