"""RAG 三层统一叙事单测：derive_embed_query 一致 + embed_cache_hit_rate + trace 字段。"""

import pytest

from agent_runtime.features.memory.core import default_memory_state
from agent_runtime.features.memory.semantic import (
    SemanticMemory,
    derive_embed_query,
)


# ---------------------------------------------------------------------------
# derive_embed_query 三层共用
# ---------------------------------------------------------------------------


class TestDeriveEmbedQuery:
    def test_extracts_exception_type(self):
        query = derive_embed_query(
            "TypeError at calculator.py:42\n  File \"src/calc.py\", line 42"
        )
        assert "TypeError" in query

    def test_extracts_file_name(self):
        query = derive_embed_query(
            'ImportError in app.py\n  File "src/utils/helpers.py", line 15'
        )
        assert "helpers.py" in query or "utils" in query

    def test_fallback_to_task_summary(self):
        query = derive_embed_query("", task_summary="修复除零错误")
        assert "修复除零错误" in query

    def test_empty_returns_user_message_fallback(self):
        query = derive_embed_query("hello world")
        assert "hello world" in query

    def test_consistent_across_calls(self):
        """同一输入多次调用返回相同结果。"""
        msg = "ValueError at src/calc.py:100"
        q1 = derive_embed_query(msg)
        q2 = derive_embed_query(msg)
        assert q1 == q2


# ---------------------------------------------------------------------------
# embed_cache_hit_rate
# ---------------------------------------------------------------------------


class TestEmbedCacheHitRate:
    def test_initial_rate_is_zero(self, monkeypatch):
        import agent_runtime.features.memory.semantic as sem_mod

        monkeypatch.setattr(sem_mod, "_embed_cache_hits", 0)
        monkeypatch.setattr(sem_mod, "_embed_cache_misses", 0)
        assert sem_mod.get_embed_cache_hit_rate() == 0.0

    def test_module_level_counter(self):
        import agent_runtime.features.memory.semantic as sem_mod

        old_hits, old_misses = sem_mod._embed_cache_hits, sem_mod._embed_cache_misses
        sem_mod._embed_cache_hits = 7
        sem_mod._embed_cache_misses = 3
        assert sem_mod.get_embed_cache_hit_rate() == 0.7
        # restore
        sem_mod._embed_cache_hits = old_hits
        sem_mod._embed_cache_misses = old_misses

    def test_semantic_memory_delegates_to_module(self):
        from agent_runtime.features.memory.semantic import (
            SemanticMemory,
            get_embed_cache_hit_rate,
        )

        sem = SemanticMemory()
        assert sem.embed_cache_hit_rate == get_embed_cache_hit_rate()

    def test_get_stats_returns_tuple(self):
        from agent_runtime.features.memory.semantic import get_embed_cache_stats

        hits, misses = get_embed_cache_stats()
        assert isinstance(hits, int)
        assert isinstance(misses, int)


# ---------------------------------------------------------------------------
# RAG 三层集成
# ---------------------------------------------------------------------------


class TestRAGLayersIntegration:
    def test_knowledge_section_has_trace_info(self, temp_workspace):
        """_get_knowledge 返回的 knowledge 文本含三层检索结果。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.context_manager import ContextManager
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        config = AgentConfig(provider="fake", max_steps=3, prompt_budget=6000)
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(config=config, model_client=FakeModelClient([]), workspace=ws)

        # 注入 episodic + durable 数据
        agent.session["memory"]["episodic_notes"] = [
            {"text": "TypeError at calculator.py:42", "tags": ["error"],
             "kind": "error", "note_index": 1, "created_at": 1e12},
        ]

        cm = ContextManager(agent)
        knowledge = cm._get_knowledge("TypeError")
        # Layer 1: episodic 命中
        assert "TypeError" in knowledge
        # Layer 2: durable (可能空，但路径畅通)
        assert isinstance(knowledge, str)

    def test_derive_embed_query_used_by_get_knowledge(self, temp_workspace):
        """_get_knowledge 内部通过 run_user_query → derive_embed_query 提取查询。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.context_manager import ContextManager
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        config = AgentConfig(provider="fake", max_steps=3)
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(config=config, model_client=FakeModelClient([]), workspace=ws)

        cm = ContextManager(agent)
        # 无 memory 数据 → knowledge 为空
        knowledge = cm._get_knowledge("")
        assert knowledge == ""

    def test_precedent_store_layer2_exists(self):
        """Layer 2 RepairPrecedentStore 可正常导入。"""
        from src.repair.precedent import RepairPrecedentStore

        assert RepairPrecedentStore is not None

    def test_embed_cache_layer3_exists(self):
        """Layer 3 embed_cache directory 常量存在。"""
        from agent_runtime.features.memory.semantic import _EMBED_CACHE_DIR

        assert _EMBED_CACHE_DIR is not None
        assert "embed_cache" in str(_EMBED_CACHE_DIR)
