"""L0 Tier Guard 单测。"""

import pytest

from agent_runtime.compression_pipeline import l0_tier_guard, run_compression_pipeline
from agent_runtime.context_manager import TokenBudget
from agent_runtime.tier_policy import (
    TierPolicy,
    filter_relevant_results,
    l0_filter_history,
)


@pytest.fixture
def budget():
    return TokenBudget(model="gpt-4", provider="openai", total_limit=6000)


@pytest.fixture
def policy():
    return TierPolicy(allowed_tools=frozenset({"read_file", "search"}))


class TestL0FilterHistory:
    def test_keeps_user_and_pinned_errors(self, policy):
        history = [
            {"role": "user", "content": "fix bug"},
            {"role": "tool", "tool_name": "read_file", "content": "Error: Traceback\nline 1"},
        ]
        filtered, stats = l0_filter_history(history, policy)
        assert len(filtered) == 2
        assert stats["dropped"] == 0

    def test_drops_empty_assistant(self, policy):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "   "},
        ]
        filtered, stats = l0_filter_history(history, policy)
        assert len(filtered) == 1
        assert stats["dropped"] == 1
        assert "empty_content" in stats["rules_applied"][0]

    def test_drops_rejected_tool_result(self, policy):
        history = [
            {"role": "tool", "tool_name": "read_file", "content": "Error: 工具 'write_file' 不在允许列表中。"},
        ]
        filtered, stats = l0_filter_history(history, policy)
        assert filtered == []
        assert stats["dropped"] == 1
        assert "rejected_tool" in stats["rules_applied"][0]

    def test_drops_disallowed_tool_name(self, policy):
        history = [
            {"role": "tool", "tool_name": "write_file", "content": "ok"},
        ]
        filtered, stats = l0_filter_history(history, policy)
        assert filtered == []
        assert "disallowed_tool" in stats["rules_applied"][0]

    def test_drops_low_value_system_noise(self, policy):
        history = [
            {"role": "system", "content": "工具调用格式错误"},
        ]
        filtered, stats = l0_filter_history(history, policy)
        assert filtered == []
        assert "low_value_system" in stats["rules_applied"][0]

    def test_does_not_mutate_canonical(self, policy):
        history = [{"role": "assistant", "content": ""}, {"role": "user", "content": "x"}]
        canonical_len = len(history)
        l0_filter_history(history, policy)
        assert len(history) == canonical_len


class TestL0TierGuardPipeline:
    def test_l0_metadata_recorded(self, budget, policy):
        history = [
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "hello"},
        ]
        meta: dict = {}
        result = l0_tier_guard(history, meta, policy=policy)
        assert len(result) == 1
        pipe = meta["compression_pipeline"]
        assert pipe["l0_dropped"] == 1
        assert pipe["l0"] == "applied"

    def test_pipeline_passes_tier_policy(self, budget, policy):
        history = [
            {"role": "tool", "tool_name": "write_file", "content": "secret"},
            {"role": "user", "content": "go"},
        ]
        meta: dict = {}
        projected = run_compression_pipeline(history, budget, metadata=meta, tier_policy=policy)
        assert all(i.get("tool_name") != "write_file" for i in projected if i.get("role") == "tool")
        assert meta["compression_pipeline"]["l0_dropped"] == 1


class TestFilterRelevantResults:
    def test_filters_low_score_episodic(self):
        policy = TierPolicy(min_relevance_score=2.0)
        results = [
            {"text": "a", "score": 3.0},
            {"text": "b", "score": 1.0},
        ]
        filtered = filter_relevant_results(results, policy)
        assert len(filtered) == 1
        assert filtered[0]["text"] == "a"

    def test_keeps_items_without_score(self):
        policy = TierPolicy(min_relevance_score=2.0)
        results = [{"text": "semantic hit"}]
        assert filter_relevant_results(results, policy) == results
