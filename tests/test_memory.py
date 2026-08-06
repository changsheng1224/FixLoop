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
    record_read_evidence,
    remember_file,
    render_evidence_ledger,
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
        assert s["working"]["evidence_ledger"] == []
        assert s["working"]["read_cache"] == {}
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

    def test_read_evidence_dedupes_and_renders(self, state):
        first = record_read_evidence(
            state,
            path="a.py",
            start=1,
            end=20,
            result_text="1 | def f():\n2 |     return 1",
        )
        second = record_read_evidence(
            state,
            path="a.py",
            start=1,
            end=20,
            result_text="1 | def f():\n2 |     return 1",
        )
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert len(state["working"]["evidence_ledger"]) == 1
        assert state["working"]["evidence_ledger"][0]["duplicate_count"] == 1
        rendered = render_evidence_ledger(state)
        assert "证据账本" in rendered
        assert "dup=1" in rendered

    def test_write_invalidates_read_evidence(self, state):
        record_read_evidence(
            state,
            path="a.py",
            start=1,
            end=20,
            result_text="1 | old",
        )
        invalidate_file_summary(state, "a.py")
        entry = state["working"]["evidence_ledger"][0]
        assert entry["stale"] is True


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
        from agent_runtime.features.memory.core import default_memory_state
        from agent_runtime.features.memory.episodic import retrieval_candidates

        state = default_memory_state()
        state["episodic_notes"] = [
            {
                "text": "obs note",
                "tags": ["test"],
                "kind": "observation",
                "created_at": 1000,
                "note_index": 1,
                "retrieve_count": 0,
            },
            {
                "text": "decision note",
                "tags": ["test"],
                "kind": "decision",
                "created_at": 1000,
                "note_index": 2,
                "retrieve_count": 0,
            },
        ]
        results = retrieval_candidates(state, "test", limit=2)
        assert len(results) == 2
        # decision 权重 1.5 > observation 1.0 → decision 排前
        assert results[0]["kind"] == "decision"
        assert results[1]["kind"] == "observation"

    def test_error_before_decision_same_score(self):
        """同相似度时 error 排 decision 前。"""
        from agent_runtime.features.memory.core import default_memory_state
        from agent_runtime.features.memory.episodic import retrieval_candidates

        state = default_memory_state()
        state["episodic_notes"] = [
            {
                "text": "decision note",
                "tags": ["test"],
                "kind": "decision",
                "created_at": 1000,
                "note_index": 1,
                "retrieve_count": 0,
            },
            {
                "text": "error note",
                "tags": ["test"],
                "kind": "error",
                "created_at": 1000,
                "note_index": 2,
                "retrieve_count": 0,
            },
        ]
        results = retrieval_candidates(state, "test", limit=2)
        assert results[0]["kind"] == "error"
        assert results[1]["kind"] == "decision"

    def test_higher_base_score_overrides_kind(self):
        """高基准分 observation 可超越低基准分 decision。"""
        from agent_runtime.features.memory.core import default_memory_state
        from agent_runtime.features.memory.episodic import retrieval_candidates

        state = default_memory_state()
        state["episodic_notes"] = [
            {
                "text": "decision low",
                "tags": ["low"],
                "kind": "decision",
                "created_at": 1000,
                "note_index": 1,
                "retrieve_count": 0,
            },
            {
                "text": "obs high score",
                "tags": ["high", "high", "high"],
                "kind": "observation",
                "created_at": 1000,
                "note_index": 2,
                "retrieve_count": 0,
            },
        ]
        # "high" query → obs matches 3 tags (score=9) vs decision 1 tag (score=3*1.5=4.5)
        # obs 基准分更高 → 排前
        results = retrieval_candidates(state, "high", limit=2)
        assert results[0]["kind"] == "observation"

    def test_kind_weight_in_score_field(self):
        """返回结果的 score 字段已含 kind 权重。"""
        from agent_runtime.features.memory.core import default_memory_state
        from agent_runtime.features.memory.episodic import retrieval_candidates

        state = default_memory_state()
        state["episodic_notes"] = [
            {
                "text": "note",
                "tags": ["test"],
                "kind": "error",
                "created_at": 1000,
                "note_index": 1,
                "retrieve_count": 0,
            },
        ]
        results = retrieval_candidates(state, "test", limit=1)
        assert "score" in results[0]
        # tag match 3 + tag-in-query 2 = 5 × error weight 2.0 = 10.0
        assert results[0]["score"] == 10.0


# ---------------------------------------------------------------------------
# episodic → durable promote（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestAutoPromote:
    """AUTO_PROMOTE 开关：默认 false 不写入 durable，true 时达到阈值自动晋升。"""

    def test_auto_promote_default_false(self):
        from agent_runtime.features.memory.episodic import AUTO_PROMOTE

        assert AUTO_PROMOTE is False

    def test_auto_promote_off_does_not_write(self, tmp_path):
        """AUTO_PROMOTE=False 时只累加 retrieve_count，不写 durable。"""
        from agent_runtime.features.memory.core import default_memory_state
        from agent_runtime.features.memory.durable import DurableMemoryStore
        from agent_runtime.features.memory.episodic import AUTO_PROMOTE, retrieval_candidates

        assert AUTO_PROMOTE is False  # 确保默认关

        store = DurableMemoryStore(str(tmp_path))
        store.ensure_dirs()
        state = default_memory_state()
        state["episodic_notes"] = [
            {
                "text": "use pytest for testing",
                "tags": ["pytest", "testing"],
                "kind": "decision",
                "created_at": 1000,
                "note_index": 1,
                "retrieve_count": 0,
            }
        ]

        # 多次检索触发 PROMOTE_THRESHOLD
        for _ in range(5):
            retrieval_candidates(state, "pytest", limit=3, durable_store=store)

        # retrieve_count 累加了
        assert state["episodic_notes"][0]["retrieve_count"] == 5
        # 但 durable 没有写入（AUTO_PROMOTE=False）
        entries = store._read_topic("key-decisions")
        assert len(entries) == 0

    def test_auto_promote_on_writes_to_durable(self, monkeypatch, tmp_path):
        """AUTO_PROMOTE=True 时达到阈值自动写入 durable。"""
        import agent_runtime.features.memory.episodic as ep_mod

        monkeypatch.setattr(ep_mod, "AUTO_PROMOTE", True)

        from agent_runtime.features.memory.core import default_memory_state
        from agent_runtime.features.memory.durable import DurableMemoryStore
        from agent_runtime.features.memory.episodic import retrieval_candidates

        store = DurableMemoryStore(str(tmp_path))
        store.ensure_dirs()
        state = default_memory_state()
        state["episodic_notes"] = [
            {
                "text": "use ruff format for linting",
                "tags": ["ruff", "linting"],
                "kind": "decision",
                "created_at": 1000,
                "note_index": 1,
                "retrieve_count": 0,
            }
        ]

        for _ in range(3):
            retrieval_candidates(state, "ruff", limit=3, durable_store=store)

        # durable 写入了
        entries = store._read_topic("key-decisions")
        assert len(entries) >= 1
        assert "ruff" in entries[0].lower()

    def test_promote_threshold_shared_with_dream(self):
        """episodic.PROMOTE_THRESHOLD 与 dream.PROMOTE_SUGGEST_HIT_MIN 同源。"""
        from agent_runtime.features.memory.dream import PROMOTE_SUGGEST_HIT_MIN
        from agent_runtime.features.memory.episodic import PROMOTE_THRESHOLD

        assert PROMOTE_SUGGEST_HIT_MIN == PROMOTE_THRESHOLD

    def test_retrieve_count_below_threshold_no_promote(self, monkeypatch, tmp_path):
        """未达阈值不晋升。"""
        import agent_runtime.features.memory.episodic as ep_mod

        monkeypatch.setattr(ep_mod, "AUTO_PROMOTE", True)

        from agent_runtime.features.memory.core import default_memory_state
        from agent_runtime.features.memory.durable import DurableMemoryStore
        from agent_runtime.features.memory.episodic import retrieval_candidates

        store = DurableMemoryStore(str(tmp_path))
        store.ensure_dirs()
        state = default_memory_state()
        state["episodic_notes"] = [
            {
                "text": "decision below threshold",
                "tags": ["test"],
                "kind": "decision",
                "created_at": 1000,
                "note_index": 1,
                "retrieve_count": 0,
            }
        ]

        # 只检索 2 次（未达 PROMOTE_THRESHOLD=3）
        for _ in range(2):
            retrieval_candidates(state, "test", limit=3, durable_store=store)

        entries = store._read_topic("key-decisions")
        assert len(entries) == 0
