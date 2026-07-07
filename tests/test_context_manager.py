"""TokenBudget + ContextManager 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import (
    KEEP_RECENT_HISTORY,
    ContextManager,
    TokenBudget,
    fit_prompt_to_budget,
)
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def budget():
    return TokenBudget(model="gpt-4", provider="openai", total_limit=6000)


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=4)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestTokenBudget:
    """TokenBudget 精确计数测试。"""

    def test_count_english(self, budget):
        n = budget.count("Hello world")
        assert n == 2

    def test_count_chinese(self, budget):
        n = budget.count("你好世界")
        assert 4 <= n <= 6  # 中文每字约 1-1.5 token

    def test_count_chinese_long(self, budget):
        text = "这是一段较长的中文文本，用于测试 token 计数的准确性。"
        chars = len(text)
        tokens = budget.count(text)
        # 中文场景 token/char 比在 0.5~2 之间，误差远小于字符数估算
        ratio = tokens / chars
        assert 0.3 < ratio < 2.5

    def test_fit_truncates(self, budget):
        text = "hello world " * 100
        tokens_before = budget.count(text)
        truncated = budget.fit(text, 10)
        assert budget.count(truncated) <= 10
        assert budget.count(truncated) < tokens_before

    def test_fit_short_text_unchanged(self, budget):
        text = "hello"
        truncated = budget.fit(text, 100)
        assert truncated == text


class TestFitPromptToBudget:
    def test_fits_long_user_under_total(self):
        system = "system prompt " * 50
        user = "user content " * 5000
        _, fitted_user, meta = fit_prompt_to_budget(system, user, total_limit=6000)
        assert meta["total_tokens"] <= 6000
        assert len(fitted_user) < len(user)

    def test_agent_fit_user_message_uses_config_budget(self, agent):
        agent.config.prompt_budget = 800
        fitted, meta = agent.fit_user_message("word " * 5000)
        assert meta["total_tokens"] <= 800
        assert len(fitted) < len("word " * 5000)


class TestContextManagerBuild:
    """ContextManager.build() 测试。"""

    def test_build_returns_prompt_and_metadata(self, agent):
        cm = ContextManager(agent)
        prompt, meta = cm.build("what is this project?")
        assert "当前任务" in prompt
        assert "what is this project?" in prompt
        assert "sections" in meta
        assert "total_tokens" in meta

    def test_request_section_never_cut(self, agent):
        cm = ContextManager(agent)
        _, meta = cm.build("hello")
        # request 在 sections 中有记录
        assert "request" in meta["sections"]

    def test_prefix_included(self, agent):
        cm = ContextManager(agent)
        prompt, _ = cm.build("test")
        assert "可用工具" in prompt
        assert "list_files" in prompt

    def test_history_present_when_multiple_rounds(self, agent):
        # 模拟多轮对话
        agent.record({"role": "user", "content": "round 1"})
        agent.record({"role": "assistant", "content": "answer 1"})
        cm = ContextManager(agent)
        prompt, _ = cm.build("round 2")
        assert "对话历史" in prompt
        assert "round 1" in prompt

    def test_super_long_history_compressed(self, agent):
        # 构造 30 轮历史
        for i in range(30):
            agent.record({"role": "user", "content": f"question {i}"})
            agent.record({"role": "tool", "content": f"result of question {i}: " + "x" * 200})
        cm = ContextManager(agent)
        prompt, meta = cm.build("new question")
        # 最近 KEEP_RECENT_HISTORY 条保留
        assert f"question {29}" in prompt
        # 总 token 在预算内
        assert meta["total_tokens"] <= 6000

    def test_budget_overflow_triggers_cuts(self, agent):
        cm = ContextManager(agent, total_budget=500)  # 极小预算
        _, meta = cm.build("test")
        # 应该发生了裁剪
        assert len(meta.get("cuts", [])) > 0


class TestHistoryCompression:
    """历史压缩测试。"""

    def test_recent_entries_preserved(self, agent):
        for i in range(KEEP_RECENT_HISTORY + 2):
            agent.record({"role": "user", "content": f"msg {i}"})
        cm = ContextManager(agent)
        history_text = cm._get_compressed_history()
        # 最近的消息保留
        assert f"msg {KEEP_RECENT_HISTORY + 1}" in history_text
        assert "早期摘要" in history_text

    def test_compressed_entries_truncated(self, agent):
        for i in range(20):
            agent.record({"role": "tool", "content": "very long result " * 100})
        cm = ContextManager(agent)
        history_text = cm._get_compressed_history()
        # 旧工具结果被压缩
        tokens = cm.budget.count(history_text)
        assert tokens < 2600
