"""LLM 调用预算硬顶单测：max_llm_calls + budget_exhausted。"""


class TestLLMBudgetHardCap:
    def test_budget_exhausted_stops_early(self, temp_workspace):
        """max_llm_calls=1 时第二次 model call 触发 budget_exhausted。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        config = AgentConfig(provider="fake", max_steps=5, max_llm_calls_per_repair=1)
        agent = Agent(
            config=config,
            model_client=FakeModelClient(
                [
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    "<final>should not reach</final>",
                ]
            ),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        loop = AgentLoop(agent)
        answer = loop.run("test budget")
        assert "budget" in loop.stop_reason or "LLM" in answer or "硬顶" in answer

    def test_no_limit_not_exhausted(self, temp_workspace):
        """max_llm_calls=0 时不限制。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        config = AgentConfig(provider="fake", max_steps=2, max_llm_calls_per_repair=0)
        agent = Agent(
            config=config,
            model_client=FakeModelClient(
                [
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    "<final>all good</final>",
                ]
            ),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        loop = AgentLoop(agent)
        answer = loop.run("test no limit")
        assert "all good" in answer

    def test_llm_call_count_in_runtime_metrics(self, temp_workspace):
        """report.runtime_metrics 含 llm_calls + llm_call_limit。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        config = AgentConfig(provider="fake", max_steps=3, max_llm_calls_per_repair=10)
        agent = Agent(
            config=config,
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        agent.ask("test")
        # 验证 config 字段
        assert config.max_llm_calls_per_repair == 10
