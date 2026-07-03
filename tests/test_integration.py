"""M1 集成测试：FakeClient 模拟完整多步工具调用，验证 Agent 全链路。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def config():
    return AgentConfig(provider="fake", max_steps=4, max_new_tokens=256)


@pytest.fixture
def workspace(temp_workspace):
    return WorkspaceContext.build(str(temp_workspace))


class TestIntegrationFullPipeline:
    """完整管线：用户输入 → 多步工具调用 → final。"""

    def test_read_file_then_respond(self, config, workspace):
        """模拟：Agent 先 read_file 再返回总结。"""
        agent = Agent(
            config=config,
            model_client=FakeModelClient(
                [
                    '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
                    "<final>README.md 描述了项目用途和安装方法。</final>",
                ]
            ),
            workspace=workspace,
        )
        answer = agent.ask("read README.md")
        assert "README" in answer
        # 验证 history 中有 tool 调用记录
        tool_records = [h for h in agent.session["history"] if h["role"] == "tool"]
        assert len(tool_records) == 1

    def test_list_then_search_then_respond(self, config, workspace):
        """模拟：list_files → search → final。"""
        agent = Agent(
            config=config,
            model_client=FakeModelClient(
                [
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    '<tool>{"name":"search","args":{"pattern":"TODO","path":"."}}</tool>',
                    "<final>发现 2 个文件，搜索到 0 个 TODO。</final>",
                ]
            ),
            workspace=workspace,
        )
        answer = agent.ask("列出文件并搜索 TODO")
        assert "TODO" in answer

    def test_format_error_then_correct(self, config, workspace):
        """模拟：格式错误 → retry → 正确调用 → final。"""
        agent = Agent(
            config=config,
            model_client=FakeModelClient(
                [
                    "this is not a valid tool or final format",
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    "<final>经过 retry 后成功完成任务。</final>",
                ]
            ),
            workspace=workspace,
        )
        answer = agent.ask("测试 retry")
        assert "成功" in answer

    def test_max_steps_limit(self, config, workspace):
        """模拟：超过 max_steps 后强制终止。"""
        config.max_steps = 2
        agent = Agent(
            config=config,
            model_client=FakeModelClient(
                [
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',  # 第 3 次，超标
                ]
            ),
            workspace=workspace,
        )
        answer = agent.ask("无限循环")
        assert "步数限制" in answer or "limit" in answer.lower()

    def test_unknown_tool_graceful(self, config, workspace):
        """模拟：调用未注册工具 → Error → 调整策略 → final。"""
        agent = Agent(
            config=config,
            model_client=FakeModelClient(
                [
                    '<tool>{"name":"delete_everything","args":{}}</tool>',
                    "<final>该工具不可用，改用其他方式完成了任务。</final>",
                ]
            ),
            workspace=workspace,
        )
        answer = agent.ask("删除所有文件")
        assert "不可用" in answer or "完成" in answer


class TestAgentCLI:
    """CLI 级集成测试。"""

    def test_cli_fake_mode(self, workspace):
        """验证 CLI --provider fake 模式端到端可用。"""
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_runtime",
                "--provider",
                "fake",
                "--cwd",
                str(workspace.repo_root),
                "hello",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # FakeClient 预设默认输出
        assert result.returncode == 0
