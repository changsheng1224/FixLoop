"""共享测试 fixtures：FakeClient, 临时 workspace 等。"""

import tempfile
import os
from pathlib import Path
import pytest

from agent_runtime.providers.clients import FakeModelClient


@pytest.fixture
def temp_workspace():
    """创建临时 git 仓库作为测试 workspace。"""
    import subprocess

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # git init
        subprocess.run(
            ["git", "init"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        # Create files
        (root / "README.md").write_text("# Test Project\n\nThis is a test repo.\n")
        (root / "pyproject.toml").write_text("[project]\nname='test'\n")
        # git add + commit
        subprocess.run(
            ["git", "add", "."],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        yield root


@pytest.fixture
def fake_client():
    """创建预设输出序列的 FakeClient。"""
    return FakeModelClient


@pytest.fixture
def non_git_dir():
    """创建非 git 目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "hello.txt").write_text("hello world")
        yield root
