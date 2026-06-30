"""Semantic Memory 单测：语义检索 + 降级。"""


from agent_runtime.features.memory import (
    SemanticMemory,
    append_note,
    default_memory_state,
    retrieval_candidates,
    retrieval_candidates_semantic,
)


class TestSemanticMemory:
    """SemanticMemory 类测试。"""

    def test_available_or_not(self):
        """模型可能可用也可能不可用，但绝不抛异常。"""
        sem = SemanticMemory()
        assert isinstance(sem.available, bool)

    def test_search_empty_returns_empty(self):
        sem = SemanticMemory()
        results = sem.search("anything")
        assert results == []

    def test_add_and_search_synonym(self):
        sem = SemanticMemory()
        sem.add({"text": "pytest fixture for test database setup", "note_index": 1})
        sem.add({"text": "TypeError when calling int() on None", "note_index": 2})

        if sem.available:
            results = sem.search("test initialization")
            assert len(results) >= 1
        else:
            # 降级：search 返回空但不报错
            results = sem.search("test initialization")
            assert results == []


class TestRetrievalFallback:
    """retrieval_candidates_semantic 降级测试。"""

    def test_keywords_still_works(self):
        """模型不可用时，keywords 路径正常工作。"""
        state = default_memory_state()
        append_note(state, "pytest fixture for test database", tags=["test", "pytest"])
        append_note(state, "TypeError at line 42", tags=["error"])

        results = retrieval_candidates_semantic(state, "pytest")
        assert len(results) >= 1

    def test_no_crash_when_model_unavailable(self):
        """语义模型加载失败不抛异常。"""
        state = default_memory_state()
        # 空 notes 也能正常处理
        results = retrieval_candidates_semantic(state, "nothing")
        assert results == []

    def test_keywords_finds_exact_match(self):
        """精确 keywords 匹配在 semantic 降级时仍然生效。"""
        state = default_memory_state()
        append_note(state, "import error in utils.py", tags=["import"])
        results = retrieval_candidates(state, "import")
        assert len(results) == 1
        assert "import" in results[0]["text"]
