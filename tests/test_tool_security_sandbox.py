"""工具安全与沙箱：敏感路径、体量、shell 违规、worktree。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.io_limits import is_likely_binary, truncate_text
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.sensitive_paths import check_sensitive_access, is_sensitive_path
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_executor import ToolExecutor
from agent_runtime.tools import tool_grep, tool_read_file, tool_run_shell, tool_write_file
from agent_runtime.worktree import create_worktree, remove_worktree, worktree_enabled


@pytest.fixture
def config():
    return AgentConfig(provider="fake", max_steps=4, approval="auto")


@pytest.fixture
def agent(config, workspace):
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=workspace)


@pytest.fixture
def executor(agent):
    return ToolExecutor(agent=agent, approval_policy="auto")


class TestSensitivePaths:
    def test_env_and_pem_detected(self):
        assert is_sensitive_path(".env")
        assert is_sensitive_path(".env.local")
        assert is_sensitive_path("secrets/id_rsa")
        assert is_sensitive_path("cert.pem")
        assert is_sensitive_path("credentials.json")
        assert not is_sensitive_path("src/main.py")

    def test_gate3_rejects_read_env(self, executor, workspace):
        (Path(workspace.repo_root) / ".env").write_text("SECRET=1\n", encoding="utf-8")
        result = executor.execute("read_file", {"path": ".env"})
        assert result.metadata["tool_status"] == "rejected"
        assert result.metadata["tool_error_code"] == "sensitive_path"
        assert result.metadata.get("sandbox_violation") is True

    def test_gate3_rejects_write_pem(self, executor):
        result = executor.execute(
            "write_file",
            {"path": "leak.pem", "content": "-----BEGIN-----\n"},
        )
        assert result.metadata["tool_error_code"] == "sensitive_path"

    def test_direct_tool_blocks_sensitive(self, workspace):
        root = Path(workspace.repo_root)
        (root / ".env").write_text("X=1\n", encoding="utf-8")
        ctx = ToolContext(root=str(root))
        assert "敏感路径" in tool_read_file(ctx, {"path": ".env"})
        assert check_sensitive_access("write_file", ".env") == "sensitive_path"


class TestIoLimits:
    def test_oversized_read_gate3(self, executor, workspace, monkeypatch):
        monkeypatch.setenv("FIXLOOP_READ_MAX_BYTES", "64")
        big = Path(workspace.repo_root) / "big.txt"
        big.write_text("x" * 200, encoding="utf-8")
        result = executor.execute("read_file", {"path": "big.txt"})
        assert result.metadata["tool_error_code"] == "oversized_read"

    def test_binary_rejected(self, executor, workspace):
        blob = Path(workspace.repo_root) / "a.bin"
        blob.write_bytes(b"\x00\x01\x02\x03" + b"\xff" * 100)
        assert is_likely_binary(blob)
        result = executor.execute("read_file", {"path": "a.bin"})
        assert result.metadata["tool_error_code"] == "binary_file"

    def test_truncate_text(self):
        text, truncated = truncate_text("abcdefghij", 4, label="t")
        assert truncated
        assert "truncated" in text


class TestPathEscapeAndShell:
    def test_path_escape_sandbox_flag(self, executor):
        result = executor.execute("read_file", {"path": "../outside.txt"})
        assert result.metadata["tool_error_code"] == "path_escape"
        assert result.metadata.get("sandbox_violation") is True

    def test_malicious_shell_gate3(self, executor):
        # sudo 在 blocklist；Gate3 早于 Gate7 deny
        result = executor.execute("run_shell", {"command": "sudo rm -rf /"})
        assert result.metadata["gate_id"] == 3
        assert result.metadata["tool_error_code"] == "sandbox_violation"

    def test_shell_allowlist_direct(self, workspace):
        ctx = ToolContext(root=str(workspace.repo_root))
        bad = tool_run_shell(ctx, {"command": "nc -l 9999"})
        assert "安全策略拒绝" in bad or "Error" in bad
        ok = tool_run_shell(ctx, {"command": "echo safe"})
        assert "exit_code: 0" in ok


class TestGrepSkipsSensitive:
    def test_grep_does_not_read_env(self, workspace):
        root = Path(workspace.repo_root)
        (root / ".env").write_text("SECRET_TOKEN=abc\n", encoding="utf-8")
        (root / "ok.py").write_text("SECRET_TOKEN = None\n", encoding="utf-8")
        ctx = ToolContext(root=str(root))
        out = tool_grep(ctx, {"pattern": "SECRET_TOKEN", "path": ".", "max_results": 20})
        assert ".env" not in out or "敏感" in out


class TestWorktree:
    def test_create_and_remove(self, temp_workspace):
        repo = Path(temp_workspace)
        handle = create_worktree(repo, "run-demo-1")
        assert handle.path.is_dir()
        assert (handle.path / "README.md").exists()
        assert remove_worktree(repo, handle.path, force=True)
        assert not handle.path.exists()

    def test_worktree_enabled_flag(self, monkeypatch):
        monkeypatch.delenv("FIXLOOP_USE_WORKTREE", raising=False)
        assert worktree_enabled() is False
        monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
        assert worktree_enabled() is True


class TestCancelDoesNotLeaveSensitiveWrite:
    def test_write_sensitive_never_succeeds(self, workspace):
        ctx = ToolContext(root=str(workspace.repo_root))
        out = tool_write_file(ctx, {"path": "id_rsa", "content": "private"})
        assert "敏感路径" in out
        assert not (Path(workspace.repo_root) / "id_rsa").exists()
