"""Semantic Memory 单测：语义检索 + 降级。"""

import os

from agent_runtime.features.memory import (
    SemanticMemory,
    append_note,
    default_memory_state,
    retrieval_candidates,
    retrieval_candidates_semantic,
)


class TestOfflineMode:
    """HF Hub 离线模式配置。"""

    def test_model_cached_locally_detects_snapshot(self, tmp_path, monkeypatch):
        from agent_runtime.features.memory import semantic

        monkeypatch.setattr(semantic, "_hf_cache_dir", lambda: tmp_path)
        snap = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}", encoding="utf-8")
        assert semantic._model_cached_locally(semantic._SEMANTIC_MODEL_ID) is True

    def test_model_cached_locally_missing(self, tmp_path, monkeypatch):
        from agent_runtime.features.memory import semantic

        monkeypatch.setattr(semantic, "_hf_cache_dir", lambda: tmp_path)
        assert semantic._model_cached_locally(semantic._SEMANTIC_MODEL_ID) is False

    def test_auto_offline_when_cache_exists(self, tmp_path, monkeypatch):
        from agent_runtime.features.memory import semantic

        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        monkeypatch.setattr(semantic, "_hf_cache_dir", lambda: tmp_path)
        snap = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}", encoding="utf-8")

        semantic._configure_hf_hub(semantic._SEMANTIC_MODEL_ID)
        assert os.environ.get("HF_HUB_OFFLINE") == "1"

    def test_respect_explicit_hf_hub_offline_false(self, tmp_path, monkeypatch):
        from agent_runtime.features.memory import semantic

        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        monkeypatch.setattr(semantic, "_hf_cache_dir", lambda: tmp_path)
        snap = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}", encoding="utf-8")

        semantic._configure_hf_hub(semantic._SEMANTIC_MODEL_ID)
        assert os.environ.get("HF_HUB_OFFLINE") == "0"


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
