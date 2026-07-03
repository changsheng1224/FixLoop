"""Bonus 新增功能测试：API 延迟、CB 状态、node_timings、token 追踪、
摘要缓存、rules 注入、retry 退避、检索分数、topic 标注、profile、health。"""

import json

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
)
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


class TestApiLatencyStats:
    """API 响应时间统计。"""

    def test_initial_stats_zero(self):
        client = AnthropicCompatibleModelClient(model="x", base_url="http://x", api_key="x")
        stats = client.latency_stats()
        assert stats["count"] == 0
        assert stats["avg"] == 0


class TestCBStatus:
    """CB 状态暴露。"""

    def test_cb_initial_state_closed(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        assert agent.circuit_breaker.state == "closed"


class TestNodeTimings:
    """node_timings 记录。"""

    def test_timings_recorded(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2, max_new_tokens=512),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        agent.ask("hello")
        runs_dir = temp_workspace / ".agent" / "runs"
        task_files = list(runs_dir.glob("*/task_state.json"))
        assert len(task_files) >= 1
        data = json.loads(task_files[0].read_text())
        assert "node_timings" in data


class TestTokenTracking:
    """token 消耗追踪。"""

    def test_report_has_token_usage(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2, max_new_tokens=512),
            model_client=FakeModelClient(["<final>done</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        agent.ask("test")
        runs_dir = temp_workspace / ".agent" / "runs"
        report_files = list(runs_dir.glob("*/report.json"))
        assert len(report_files) >= 1
        data = json.loads(report_files[0].read_text())
        assert "token_usage" in data
        assert isinstance(data["token_usage"], dict)


class TestSummaryCache:
    """摘要缓存。"""

    def test_cache_prevents_duplicate_calls(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2),
            model_client=FakeModelClient(["summary from cache"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        from agent_runtime.context_manager import ContextManager

        cm = ContextManager(agent)
        assert cm._summary_cache == {}

        history = [{"role": "user", "content": f"msg {i}: " + "x" * 200} for i in range(20)]
        result1 = cm._maybe_summarize_history(history, trigger_tokens=10)
        # 第一次调用了 model → cache 有值
        assert len(cm._summary_cache) >= 1

        result2 = cm._maybe_summarize_history(history, trigger_tokens=10)
        # 第二次应命中缓存 → 结果相同
        assert result1 == result2


class TestRetryBackoff:
    """retry 指数退避。"""

    def test_retry_count_increments(self, temp_workspace):
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=3, max_new_tokens=512),
            model_client=FakeModelClient(
                [
                    "garbage",  # retry 1
                    "garbage",  # retry 2
                    "<final>finally ok</final>",
                ]
            ),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        answer = agent.ask("test")
        assert "ok" in answer
        # retry 记录应该存在
        sys_msgs = [h for h in agent.session["history"] if h["role"] == "system"]
        assert len(sys_msgs) >= 2


class TestRetrievalScore:
    """检索结果带分数。"""

    def test_score_included_in_results(self):
        from agent_runtime.features.memory import append_note, default_memory_state
        from agent_runtime.features.memory.episodic import retrieval_candidates

        state = default_memory_state()
        append_note(state, "TypeError at line 42", tags=["error"])
        results = retrieval_candidates(state, "TypeError")
        assert len(results) >= 1
        assert "score" in results[0]
        assert results[0]["score"] > 0


class TestTopicLabel:
    """检索返回 topic 标注。"""

    def test_topic_in_retrieval_result(self, temp_workspace):
        from agent_runtime.features.memory.durable import DurableMemoryStore

        store = DurableMemoryStore(root=str(temp_workspace))
        store.promote([("key-decisions", "Decision: use pytest")])
        results = store.retrieval("pytest")
        assert len(results) >= 1
        assert results[0]["topic"] == "key-decisions"
        assert "pytest" in results[0]["text"]


class TestProfile:
    """--profile 配置 preset。"""

    def test_ci_profile_sets_quota_zero(self, temp_workspace):
        from agent_runtime.cli import _apply_profile

        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        _apply_profile(agent, "ci")
        assert agent.config.approval == "never"
        assert agent.quota._limits["write"] == 0
        assert agent.dry_run is True

    def test_dev_profile_sets_auto_approval(self, temp_workspace):
        from agent_runtime.cli import _apply_profile

        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=WorkspaceContext.build(str(temp_workspace)),
        )
        _apply_profile(agent, "dev")
        assert agent.config.approval == "auto"
        assert agent.quota._limits["write"] == 100


class TestHealthCheck:
    """--health 健康检查。"""

    def test_health_output_is_json(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "agent_runtime", "--health"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        assert "status" in data
        assert "python" in data
        assert "git" in data
