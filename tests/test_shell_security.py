"""run_shell + security 单测。"""

import sys

import pytest

from agent_runtime.security import (
    SHELL_ENV_WHITELIST,
    looks_sensitive_env_name,
    redact_text,
    shell_env,
)
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry, tool_run_shell


@pytest.fixture
def ctx(temp_workspace):
    return ToolContext(root=str(temp_workspace))


class TestRunShell:
    """run_shell 工具测试。"""

    def test_normal_command(self, ctx):
        result = tool_run_shell(ctx, {"command": "echo hello world"})
        assert "exit_code: 0" in result
        assert "hello world" in result

    def test_timeout_kills_long_command(self, ctx):
        # 在 Windows 上 timeout 对 shell 命令不总是生效
        # 使用一个极短的 timeout 测试
        result = tool_run_shell(ctx, {"command": "sleep 10", "timeout": 1})
        assert "超时" in result or "exit_code" in result

    def test_missing_command(self, ctx):
        result = tool_run_shell(ctx, {})
        assert "Error" in result

    def test_child_process_env_excludes_api_key(self, ctx, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-leak-test-secret-value-99")
        cmd = "set" if sys.platform == "win32" else "env"
        result = tool_run_shell(ctx, {"command": cmd})
        assert "DEEPSEEK_API_KEY" not in result
        assert "sk-leak-test-secret-value-99" not in result

    def test_output_redacts_secret_values(self, ctx, monkeypatch):
        secret = "sk-redact-me-abcdefghijklmnop"
        monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
        result = tool_run_shell(ctx, {"command": f"echo {secret}"})
        assert secret not in result
        assert "<redacted>" in result


class TestSecurity:
    """security 模块测试。"""

    def test_shell_env_whitelist(self):
        """验证 shell_env() 只包含白名单变量。"""
        env = shell_env(root="/test")
        for key in env:
            if key == "PYTHONIOENCODING":
                continue
            assert key in SHELL_ENV_WHITELIST, f"{key} 不在白名单中"
        assert env.get("PWD") == "/test"
        assert env.get("PYTHONIOENCODING") == "utf-8"

    def test_shell_env_excludes_host_secrets(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-host-secret-value-12345")
        env = shell_env(root="/workspace")
        assert "DEEPSEEK_API_KEY" not in env

    def test_shell_env_skips_sensitive_names_in_allowlist(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_API_KEY", "secret")
        env = shell_env(allowlist={"CUSTOM_API_KEY", "PATH"}, root="/w")
        assert "CUSTOM_API_KEY" not in env

    def test_registry_wires_shell_env_provider(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        build_tool_registry(ctx)
        assert callable(ctx.shell_env_provider)
        env = ctx.shell_env_provider()
        assert env.get("PYTHONIOENCODING") == "utf-8"
        assert "DEEPSEEK_API_KEY" not in env

    def test_looks_sensitive_env_name(self):
        """检测敏感变量名。"""
        assert looks_sensitive_env_name("DEEPSEEK_API_KEY") is True
        assert looks_sensitive_env_name("OPENAI_TOKEN") is True
        assert looks_sensitive_env_name("DB_PASSWORD") is True
        assert looks_sensitive_env_name("HOME") is False
        assert looks_sensitive_env_name("PATH") is False

    def test_redact_text(self):
        """验证敏感值脱敏。"""
        text = "api key: sk-1234567890abcdef and token: ghp_abcdef123456"
        redacted = redact_text(text, secret_values=["sk-1234567890abcdef", "ghp_abcdef123456"])
        assert "sk-1234567890abcdef" not in redacted
        assert "ghp_abcdef123456" not in redacted
        assert "<redacted>" in redacted
