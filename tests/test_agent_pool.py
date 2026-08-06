"""Agent 池化/预热 单元测试（V1.4-Bonus1：Agent运行时）。

覆盖：
- WarmContext 基本功能
- 分词器预热与模块级缓存
- Agent warm_context 参数（向后兼容）
- 并行 Agent 构建（ThreadPoolExecutor）
- wire_orchestrator 预热路径
"""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tokenizers import clear_token_counter_cache, resolve_token_counter
from agent_runtime.warm_context import WarmContext, create_warm_context
from agent_runtime.workspace import WorkspaceContext

# ---------------------------------------------------------------------------
# WarmContext 基本功能
# ---------------------------------------------------------------------------


class TestWarmContextBasic:
    """WarmContext 构造与属性。"""

    def test_default_values(self):
        wc = WarmContext()
        assert wc.model == "deepseek-v4-pro"
        assert wc.provider == "deepseek"
        assert wc._tokenizer_warmed is False

    def test_custom_model_provider(self):
        wc = WarmContext(model="gpt-4", provider="openai")
        assert wc.model == "gpt-4"
        assert wc.provider == "openai"

    def test_repr_excludes_internal(self):
        wc = WarmContext()
        r = repr(wc)
        assert "_tokenizer_warmed" not in r


class TestWarmContextTokenizer:
    """分词器预热与缓存验证。"""

    def setup_method(self):
        clear_token_counter_cache()

    def teardown_method(self):
        clear_token_counter_cache()

    def test_warm_tokenizer_loads_into_cache(self):
        """预热后 tokenizer 命中模块级缓存。"""
        wc = WarmContext(model="deepseek-v4-pro", provider="deepseek")
        assert wc._tokenizer_warmed is False

        wc.warm_tokenizer()

        assert wc._tokenizer_warmed is True
        # 验证已加载到模块级缓存
        counter = resolve_token_counter("deepseek-v4-pro", "deepseek")
        assert counter is not None
        assert counter.backend != ""

    def test_warm_tokenizer_idempotent(self):
        """重复预热不报错，状态不变。"""
        wc = WarmContext(model="deepseek-v4-pro", provider="deepseek")
        wc.warm_tokenizer()
        assert wc._tokenizer_warmed is True

        # 第二次调用：幂等
        wc.warm_tokenizer()
        assert wc._tokenizer_warmed is True

    def test_warm_all_includes_tokenizer(self):
        """warm_all() 包含分词器预热。"""
        wc = WarmContext(model="deepseek-v4-pro", provider="deepseek")
        wc.warm_all()
        assert wc._tokenizer_warmed is True

    def test_cache_hit_after_warm(self):
        """预热后 resolve_token_counter 返回同一实例。"""
        wc = WarmContext(model="deepseek-v4-pro", provider="deepseek")
        wc.warm_tokenizer()

        c1 = resolve_token_counter("deepseek-v4-pro", "deepseek")
        c2 = resolve_token_counter("deepseek-v4-pro", "deepseek")
        assert c1 is c2  # 模块级缓存：同一实例


class TestCreateWarmContext:
    """工厂函数测试。"""

    def setup_method(self):
        clear_token_counter_cache()

    def teardown_method(self):
        clear_token_counter_cache()

    def test_returns_warm_context(self):
        wc = create_warm_context()
        assert isinstance(wc, WarmContext)

    def test_already_warmed(self):
        """create_warm_context 返回已完成预热的实例。"""
        wc = create_warm_context()
        assert wc._tokenizer_warmed is True

    def test_custom_args(self):
        wc = create_warm_context(model="gpt-4o", provider="openai")
        assert wc.model == "gpt-4o"
        assert wc.provider == "openai"
        assert wc._tokenizer_warmed is True


# ---------------------------------------------------------------------------
# Agent warm_context 参数（向后兼容）
# ---------------------------------------------------------------------------


class TestAgentWarmContext:
    """Agent 构造器 warm_context 参数。"""

    def setup_method(self):
        clear_token_counter_cache()

    def teardown_method(self):
        clear_token_counter_cache()

    def test_agent_without_warm_context(self):
        """不传 warm_context：向后兼容，Agent 正常构造。"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
            )
            assert agent._warm_context is None
            # 基本功能正常
            assert agent._prefix is not None

    def test_agent_with_warm_context(self):
        """传入 warm_context：Agent 存储引用。"""
        wc = create_warm_context(model="deepseek-v4-pro", provider="deepseek")
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
                warm_context=wc,
            )
            assert agent._warm_context is wc
            assert agent._warm_context._tokenizer_warmed is True

    def test_agent_with_unwarmed_context(self):
        """传入未预热的 WarmContext：Agent 仍正常构造。"""
        wc = WarmContext(model="deepseek-v4-pro", provider="deepseek")
        assert wc._tokenizer_warmed is False
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
                warm_context=wc,
            )
            assert agent._warm_context is wc


# ---------------------------------------------------------------------------
# 并行 Agent 构建（ThreadPoolExecutor）
# ---------------------------------------------------------------------------


class TestParallelAgentCreation:
    """ThreadPoolExecutor 并行创建 Agent。"""

    def setup_method(self):
        clear_token_counter_cache()

    def teardown_method(self):
        clear_token_counter_cache()

    def test_parallel_create_two_agents(self):
        """并行创建两个 Agent：无异常，各自独立。"""
        wc = create_warm_context(model="deepseek-v4-pro", provider="deepseek")
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)

            def build_agent(name: str) -> Agent:
                return Agent(
                    config=AgentConfig(),
                    model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                    workspace=ws,
                    cwd=tmp,
                    agent_name=name,
                    warm_context=wc,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(build_agent, "agent-a")
                fut_b = pool.submit(build_agent, "agent-b")
                agent_a = fut_a.result()
                agent_b = fut_b.result()

            assert agent_a._agent_name == "agent-a"
            assert agent_b._agent_name == "agent-b"
            # 两者共享同一个 warm_context
            assert agent_a._warm_context is agent_b._warm_context

    def test_parallel_agents_have_independent_sessions(self):
        """并行创建的 Agent 各自持有独立 session。"""
        wc = create_warm_context(model="deepseek-v4-pro", provider="deepseek")
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)

            def build_agent(name: str) -> Agent:
                return Agent(
                    config=AgentConfig(),
                    model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                    workspace=ws,
                    cwd=tmp,
                    agent_name=name,
                    warm_context=wc,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(build_agent, "a")
                fut_b = pool.submit(build_agent, "b")
                agent_a = fut_a.result()
                agent_b = fut_b.result()

            # session ID 不同
            assert agent_a.session["id"] != agent_b.session["id"]

    def test_parallel_tokenizer_is_cache_safe(self):
        """并行线程中调用 resolve_token_counter 命中同一缓存。"""
        create_warm_context(model="deepseek-v4-pro", provider="deepseek")
        results = []

        def get_counter():
            c = resolve_token_counter("deepseek-v4-pro", "deepseek")
            results.append(id(c))
            return c

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(get_counter)
            f2 = pool.submit(get_counter)
            f1.result()
            f2.result()

        # 所有线程命中同一缓存实例
        assert len(set(results)) == 1


# ---------------------------------------------------------------------------
# wire_orchestrator 预热路径（集成测试）
# ---------------------------------------------------------------------------


class TestWireOrchestratorWarmContext:
    """repair_factory.wire_orchestrator 预热集成。"""

    def setup_method(self):
        clear_token_counter_cache()

    def teardown_method(self):
        clear_token_counter_cache()

    def test_wire_orchestrator_creates_with_warm_context(self, tmp_path: Path):
        """wire_orchestrator 使用 WarmContext 并行创建 Agent。"""
        from src.orchestrator import Orchestrator
        from src.repair_factory import wire_orchestrator

        orch = wire_orchestrator(
            FakeModelClient(outputs=["<final>ok</final>"]),
            str(tmp_path),
            skip_verify=True,
            dry_run=True,
        )
        assert isinstance(orch, Orchestrator)
        assert orch.patcher is not None
        assert orch.patcher._warm_context is not None

    def test_wire_orchestrator_no_warm_context_leak(self, tmp_path: Path):
        """warm_context 不在 session 中持久化（不污染序列化）。"""
        from src.repair_factory import wire_orchestrator

        orch = wire_orchestrator(
            FakeModelClient(outputs=["<final>ok</final>"]),
            str(tmp_path),
            skip_verify=True,
            dry_run=True,
        )
        # warm_context 不影响 session 序列化
        session = orch.patcher.session
        assert "warm_context" not in session
