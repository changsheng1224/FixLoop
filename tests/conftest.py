"""共享测试 fixtures：FakeClient, 临时 workspace 等。"""

import os

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from tests.repair_support import build_repository


@pytest.fixture(autouse=True)
def isolate_repository_discovery(tmp_path, monkeypatch):
    """A non-Git fixture must not inherit the developer's enclosing repository."""
    ceiling = str(tmp_path.parent.resolve())
    existing = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES", os.pathsep.join(filter(None, (existing, ceiling)))
    )


@pytest.fixture
def temp_workspace(tmp_path):
    """A committed repository with pytest-managed lifetime."""
    return build_repository(
        tmp_path / "workspace",
        {
            "README.md": "# Test Project\n\nThis is a test repo.\n",
            "pyproject.toml": "[project]\nname='test'\n",
        },
        git=True,
    )


@pytest.fixture
def fake_client():
    """创建预设输出序列的 FakeClient。"""
    return FakeModelClient


@pytest.fixture
def workspace(temp_workspace):
    """WorkspaceContext 包装 temp_workspace。"""
    return WorkspaceContext.build(str(temp_workspace))


@pytest.fixture
def ws(workspace):
    """与 workspace 相同，兼容旧测试命名。"""
    return workspace


@pytest.fixture
def non_git_dir(tmp_path, monkeypatch):
    root = tmp_path / "non-git"
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    return build_repository(root, {"hello.txt": "hello world"})
