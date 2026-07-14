"""三层记忆系统单测：Working Memory + Episodic Memory。"""

import pytest

from agent_runtime.features.memory import (
    MAX_EPISODIC_NOTES,
    MAX_FILE_SUMMARIES,
    MAX_RECENT_FILES,
    append_note,
    default_memory_state,
    invalidate_file_summary,
    normalize_memory_state,
    remember_file,
    retrieval_candidates,
    set_file_summary,
    set_task_summary,
)


@pytest.fixture
def state():
    """初始空记忆状态。"""
    return default_memory_state()


class TestDefaultState:
    """初始状态测试。"""

    def test_default_structure(self):
        s = default_memory_state()
        assert "working" in s
        assert s["working"]["task_summary"] == ""
        assert s["working"]["recent_files"] == []
        assert s["episodic_notes"] == []
        assert s["file_summaries"] == {}
        assert s["next_note_index"] == 0

    def test_normalize_none(self):
        s = normalize_memory_state(None, ".")
        assert s["working"]["recent_files"] == []

    def test_normalize_old_format(self):
        s = normalize_memory_state({}, ".")
        assert "working" in s
        assert "episodic_notes" in s


class TestWorkingMemory:
    """Working Memory 层测试。"""

    def test_set_task_summary(self, state):
        set_task_summary(state, "排查 calculator.py 的 TypeError")
        assert "TypeError" in state["working"]["task_summary"]

    def test_summary_truncated(self, state):
        long_msg = "x" * 500
        set_task_summary(state, long_msg)
        assert len(state["working"]["task_summary"]) <= 300

    def test_remember_file_appends(self, state):
        remember_file(state, "a.py")
        remember_file(state, "b.py")
        assert state["working"]["recent_files"] == ["a.py", "b.py"]

    def test_remember_file_deduplicates(self, state):
        remember_file(state, "a.py")
        remember_file(state, "b.py")
        remember_file(state, "a.py")  # 重复 → 移到末尾
        assert state["working"]["recent_files"] == ["b.py", "a.py"]

    def test_remember_file_trim(self, state):
        for i in range(MAX_RECENT_FILES + 5):
            remember_file(state, f"file_{i}.py")
        assert len(state["working"]["recent_files"]) == MAX_RECENT_FILES
        # 最早的文件被淘汰
        assert "file_0.py" not in state["working"]["recent_files"]
        assert f"file_{MAX_RECENT_FILES + 4}.py" in state["working"]["recent_files"]

    def test_set_file_summary(self, state):
        set_file_summary(state, "a.py", "This file defines the Agent class.")
        assert "a.py" in state["file_summaries"]
        assert "Agent" in state["file_summaries"]["a.py"]["summary"]

    def test_invalidate_file_summary(self, state):
        set_file_summary(state, "a.py", "summary")
        invalidate_file_summary(state, "a.py")
        assert "a.py" not in state["file_summaries"]


class TestEpisodicMemory:
    """Episodic Memory 层测试。"""

    def test_append_note(self, state):
        append_note(state, "read_file returned 200 lines", tags=["read", "file"], source="a.py")
        assert len(state["episodic_notes"]) == 1
        assert state["episodic_notes"][0]["kind"] == "observation"
        assert state["episodic_notes"][0]["note_index"] == 0

    def test_append_note_deduplicates(self, state):
        text = "identical note content"
        append_note(state, text)
        append_note(state, text)  # 相同内容 → 去重
        assert len(state["episodic_notes"]) == 1

    def test_append_note_different_allowed(self, state):
        append_note(state, "note 1")
        append_note(state, "note 2")
        assert len(state["episodic_notes"]) == 2

    def test_append_note_trim(self, state):
        for i in range(MAX_EPISODIC_NOTES + 5):
            append_note(state, f"note {i}")
        assert len(state["episodic_notes"]) == MAX_EPISODIC_NOTES
        # 最早的笔记被淘汰，最新的保留
        assert state["episodic_notes"][-1]["text"].startswith("note ")

    def test_retrieval_by_tag(self, state):
        append_note(
            state, "TypeError at calculator.py:42", tags=["type_error", "python"], source="calc.py"
        )
        append_note(state, "ImportError: missing module", tags=["import_error"], source="app.py")
        results = retrieval_candidates(state, "type_error")
        assert len(results) == 1
        assert "TypeError" in results[0]["text"]

    def test_retrieval_by_keyword(self, state):
        append_note(state, "fixed the pydantic validation bug")
        results = retrieval_candidates(state, "pydantic")
        assert len(results) == 1
        assert "pydantic" in results[0]["text"]

    def test_retrieval_empty(self, state):
        results = retrieval_candidates(state, "nothing")
        assert results == []

    def test_retrieval_limit(self, state):
        for i in range(5):
            append_note(state, f"test result {i}", tags=["test"])
        results = retrieval_candidates(state, "test", limit=2)
        assert len(results) == 2


class TestNormalizeMemory:
    """normalize_memory_state 测试。"""

    def test_trims_file_summaries(self, temp_workspace):
        s = default_memory_state()
        for i in range(MAX_FILE_SUMMARIES + 3):
            set_file_summary(s, f"f{i}.py", f"summary {i}")
        s = normalize_memory_state(s, str(temp_workspace))
        assert len(s["file_summaries"]) <= MAX_FILE_SUMMARIES


# ---------------------------------------------------------------------------
# episodic kind 权重（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestKindWeights:
    """KIND_WEIGHTS 权重：error(2.0) > decision(1.5) > observation(1.0)。"""

    def test_weights_defined(self):
        from agent_runtime.features.memory.episodic import KIND_WEIGHTS

        assert KIND_WEIGHTS["error"] == 2.0
        assert KIND_WEIGHTS["decision"] == 1.5
        assert KIND_WEIGHTS["observation"] == 1.0

    def test_error_ranks_higher_than_observation(self):
        """同相似度时 error 权重最高。"""
        assert 2.0 > 1.0
        assert 2.0 > 1.5

    def test_decision_ranks_between(self):
        """decision 权重在 error 和 observation 之间。"""
        assert 2.0 > 1.5 > 1.0


class TestKindWeightRetrieval:
    """retrieval_candidates 应用 kind 权重排序。"""

    def test_decision_before_observation_same_score(self):
        """同相似度时 decision 排 observation 前。"""
        from agent_runtime.features.memory.episodic import retrieval_candidates
        from agent_runtime.features.memory.core import default_memory_state

        state = default_memory_state()
        state["episodic_notes"] = [
            {"text": "obs note", "tags": ["test"], "kind": "observation",
             "created_at": 1000, "note_index": 1, "retrieve_count": 0},
            {"text": "decision note", "tags": ["test"], "kind": "decision",
             "created_at": 1000, "note_index": 2, "retrieve_count": 0},
        ]
        results = retrieval_candidates(state, "test", limit=2)
        assert len(results) == 2
        # decision 权重 1.5 > observation 1.0 → decision 排前
        assert results[0]["kind"] == "decision"
        assert results[1]["kind"] == "observation"

    def test_error_before_decision_same_score(self):
        """同相似度时 error 排 decision 前。"""
        from agent_runtime.features.memory.episodic import retrieval_candidates
        from agent_runtime.features.memory.core import default_memory_state

        state = default_memory_state()
        state["episodic_notes"] = [
            {"text": "decision note", "tags": ["test"], "kind": "decision",
             "created_at": 1000, "note_index": 1, "retrieve_count": 0},
            {"text": "error note", "tags": ["test"], "kind": "error",
             "created_at": 1000, "note_index": 2, "retrieve_count": 0},
        ]
        results = retrieval_candidates(state, "test", limit=2)
        assert results[0]["kind"] == "error"
        assert results[1]["kind"] == "decision"

    def test_higher_base_score_overrides_kind(self):
        """高基准分 observation 可超越低基准分 decision。"""
        from agent_runtime.features.memory.episodic import retrieval_candidates
        from agent_runtime.features.memory.core import default_memory_state

        state = default_memory_state()
        state["episodic_notes"] = [
            {"text": "decision low", "tags": ["low"], "kind": "decision",
             "created_at": 1000, "note_index": 1, "retrieve_count": 0},
            {"text": "obs high score", "tags": ["high", "high", "high"],
             "kind": "observation",
             "created_at": 1000, "note_index": 2, "retrieve_count": 0},
        ]
        # "high" query → obs matches 3 tags (score=9) vs decision 1 tag (score=3*1.5=4.5)
        # obs 基准分更高 → 排前
        results = retrieval_candidates(state, "high", limit=2)
        assert results[0]["kind"] == "observation"

    def test_kind_weight_in_score_field(self):
        """返回结果的 score 字段已含 kind 权重。"""
        from agent_runtime.features.memory.episodic import retrieval_candidates
        from agent_runtime.features.memory.core import default_memory_state

        state = default_memory_state()
        state["episodic_notes"] = [
            {"text": "note", "tags": ["test"], "kind": "error",
             "created_at": 1000, "note_index": 1, "retrieve_count": 0},
        ]
        results = retrieval_candidates(state, "test", limit=1)
        assert "score" in results[0]
        # tag match 3 + tag-in-query 2 = 5 × error weight 2.0 = 10.0
        assert results[0]["score"] == 10.0
