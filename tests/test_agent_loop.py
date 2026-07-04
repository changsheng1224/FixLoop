"""AgentLoop + Agent.ask() 单测：控制循环的完整验证。

使用 FakeModelClient 预设输出序列，不调真实 API。
"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent


@pytest.fixture
def config():
    return AgentConfig(provider="fake", max_steps=3, max_new_tokens=256)


def _make_agent(outputs: list[str], config, workspace):
    """快速创建一个使用 FakeClient 的 Agent。"""
    client = FakeModelClient(outputs)
    return Agent(config=config, model_client=client, workspace=workspace)


class TestAgentAsk:
    """Agent.ask() 集成测试。"""

    def test_single_tool_then_final(self, config, workspace):
        """Agent 调一次 tool 后返回 final。"""
        agent = _make_agent(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>找到 2 个文件</final>",
            ],
            config,
            workspace,
        )
        answer = agent.ask("列出文件")
        assert "找到" in answer
        assert agent.tool_context is not None

    def test_multi_tool_chain(self, config, workspace):
        """Agent 连续调用 3 次 tool 后返回 final。"""
        outputs = [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            '<tool>{"name":"search","args":{"pattern":"test","path":"."}}</tool>',
            "<final>搜索完成，共找到 5 处匹配</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("全面分析")
        assert "搜索" in answer

    def test_final_only_no_tools(self, config, workspace):
        """Agent 直接返回 final，不调任何工具。"""
        agent = _make_agent(
            ["<final>你好！我来帮你分析代码。</final>"],
            config,
            workspace,
        )
        answer = agent.ask("你好")
        assert "你好" in answer


class TestAgentLoopStopConditions:
    """AgentLoop 停机条件测试。"""

    def test_stops_at_max_steps(self, config, workspace):
        """达到 max_steps 后强制停机。"""
        # 一直返回 tool 调用，永不返回 final
        infinite_tools = [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
        ]
        agent = _make_agent(infinite_tools, config, workspace)
        answer = agent.ask("一直循环")
        # FakeClient 耗尽会抛 RuntimeError，但如果 max_steps 先到则正常停机
        # max_steps=3，3 次 tool 后应停机
        assert "步数限制" in answer or "maximum tool steps" in answer.lower()

    def test_retry_on_bad_format(self, config, workspace):
        """格式错误 → retry → 最后返回 final。"""
        outputs = [
            "garbage output without proper format",
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>重试后成功了</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("测试重试")
        assert "成功" in answer
        # 应该有 retry 记录
        history_roles = [h["role"] for h in agent.session["history"]]
        assert "system" in history_roles  # retry 通知以 system 角色记录

    def test_empty_model_response(self, config, workspace):
        """空响应被视为格式错误 → retry。"""
        outputs = [
            "",
            "<final>第二次成功了</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("测试空响应")
        assert "成功" in answer


class TestAgentSession:
    """Agent 会话状态测试。"""

    def test_history_accumulates(self, config, workspace):
        """多轮 ask 后 history 累积。"""
        agent = _make_agent(
            [
                "<final>第一轮</final>",
                "<final>第二轮</final>",
            ],
            config,
            workspace,
        )
        agent.ask("问1")
        agent.ask("问2")
        # 应该有 2 轮交互的历史
        history = agent.session["history"]
        user_msgs = [h for h in history if h["role"] == "user"]
        assert len(user_msgs) == 2

    def test_unknown_tool_handled(self, config, workspace):
        """调用未注册工具 → 返回 Error 信息 → 继续循环。"""
        outputs = [
            '<tool>{"name":"non_existent_tool","args":{}}</tool>',
            "<final>工具不存在，但已优雅处理</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("调未知工具")
        assert "处理" in answer


class TestCompleteOnce:
    def test_uses_system_prompt_without_agent_loop(self, config, workspace):
        client = FakeModelClient(["<final>[]</final>"])
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            system_prompt="You are patcher",
        )
        result = agent.complete_once("fix the bug")
        assert result == "<final>[]</final>"
        assert len(client.prompts) == 1
        assert "You are patcher" in client.prompts[0]
        assert "fix the bug" in client.prompts[0]
