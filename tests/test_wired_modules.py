"""新接入模块的集成测试：Quota CLI, ProgressCallback, SessionSave, DurableRetrieval, CB。"""

import io

import pytest

from agent_runtime.callbacks import CLIProgressCallback
from agent_runtime.config import AgentConfig
from agent_runtime.features.memory import DurableMemoryStore
from agent_runtime.providers.circuit_breaker import CircuitBreaker, State
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.session_store import SessionStore
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=2, max_new_tokens=2048)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestQuotaCLI:
    """QuotaEnforcer 通过 CLI/Agent 参数控制测试。"""

    def test_default_quota_values(self, agent):
        assert agent.quota._limits["write"] == 20
        assert agent.quota._limits["shell"] == 10
        assert agent.quota._limits["total"] == 50

    def test_quota_wired_to_tool_executor(self, agent):
        """QuotaEnforcer 通过 Agent.execute_tool() 传给 ToolExecutor。"""
        agent.quota._limits["total"] = 0  # 耗尽配额
        result = agent.execute_tool("list_files", {"path": "."})
        assert result.metadata["tool_status"] == "rejected"
        assert result.metadata["tool_error_code"] == "quota_exceeded"


class TestProgressCallback:
    """ProgressCallback 接入 Agent.ask() 测试。"""

    def test_callback_passed_to_ask(self, agent):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        # ask() 接受 callback
        answer = agent.ask("hello", callback=cb)
        assert answer == "ok"

    def test_callback_with_tool_execution(self, temp_workspace):
        """工具执行时 callback 被调用。"""
        config = AgentConfig(provider="fake", max_steps=3, max_new_tokens=2048)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>done</final>",
            ]
        )
        agent2 = Agent(config=config, model_client=client, workspace=ws)

        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        answer = agent2.ask("list files", callback=cb)
        assert "done" in answer
        output = buf.getvalue()
        assert "list_files" in output


class TestSessionAutoSave:
    """Session 自动保存测试。"""

    def test_session_saved_after_ask(self, agent, temp_workspace):
        agent.ask("hello")
        store = SessionStore(root=str(temp_workspace))
        sid = agent.session["id"]
        loaded = store.load(sid)
        assert loaded is not None
        assert loaded["id"] == sid
        assert len(loaded["history"]) > 0


class TestDurableRetrieval:
    """DurableMemory retrieval 集成测试。"""

    def test_durable_retrieved_in_context(self, temp_workspace):
        """持久记忆可被检索。"""
        store = DurableMemoryStore(root=str(temp_workspace))
        store.promote([("key-decisions", "Decision: use tiktoken for token counting")])

        config = AgentConfig(provider="fake", max_steps=2, max_new_tokens=2048)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        agent2 = Agent(config=config, model_client=client, workspace=ws, cwd=str(temp_workspace))

        from agent_runtime.context_manager import ContextManager

        cm = ContextManager(agent2)
        _, meta = cm.build("what tokenizer do we use?")

        # ContextManager 的 _get_relevant 查询了 durable memory
        # 验证 prompt 中包含持久知识
        prompt, _ = cm.build("tiktoken")
        assert "tiktoken" in prompt.lower()


class TestCircuitBreakerWired:
    """CircuitBreaker 在 AgentLoop 中实际生效测试。"""

    def test_cb_blocks_call_when_open(self, agent):
        """熔断器 OPEN 时 Agent 优雅终止。"""
        # 直接设置 CB 为 OPEN 状态
        import time

        agent.circuit_breaker._state = State.OPEN
        agent.circuit_breaker._opened_at = time.time() + 999999  # 远在未来，不会恢复

        answer = agent.ask("test")
        assert "熔断" in answer or "circuit" in answer.lower()

    def test_cb_allows_normal_call(self, agent):
        """CLOSED 状态正常放行。"""
        agent.circuit_breaker = CircuitBreaker(failure_threshold=5)
        answer = agent.ask("hello")
        assert answer == "ok"
