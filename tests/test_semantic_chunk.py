"""语料 chunk + max-pool 检索单测（V1.4-Bonus9a）。"""

from __future__ import annotations

from agent_runtime.features.memory.semantic import (
    ChunkedMemoryNote,
    _chunk_text,
)

# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_short_text_unchanged(self):
        text = "hello world"
        chunks = _chunk_text(text, max_chars=800)
        assert len(chunks) == 1
        assert chunks[0] == "hello world"

    def test_exact_boundary(self):
        text = "a" * 100
        chunks = _chunk_text(text, max_chars=100)
        assert len(chunks) == 1

    def test_exceeds_boundary_splits(self):
        text = "a" * 500 + "\n" + "b" * 500
        chunks = _chunk_text(text, max_chars=800)
        assert len(chunks) >= 2
        assert all(len(c) <= 800 for c in chunks)

    def test_splits_on_newline_preferred(self):
        text = "first paragraph\nsecond paragraph\nthird"
        chunks = _chunk_text(text, max_chars=30)
        assert len(chunks) >= 2

    def test_no_whitespace_falls_back(self):
        text = "x" * 500 + "y" * 500
        chunks = _chunk_text(text, max_chars=800)
        assert len(chunks) >= 2

    def test_empty_text(self):
        assert _chunk_text("", max_chars=100) == [""]
        assert _chunk_text("   ", max_chars=100) == [""]


# ---------------------------------------------------------------------------
# ChunkedMemoryNote
# ---------------------------------------------------------------------------


class FakeModel:
    """Fake embedding model for testing."""

    def encode(self, text: str):
        import numpy as np

        # Deterministic "embedding" from text hash
        h = hash(text) % 1024
        vec = np.array([float(h % 100) / 100], dtype=np.float32)
        return vec


class TestChunkedMemoryNote:
    def test_single_chunk_short_text(self):
        model = FakeModel()
        note_data = {"text": "short text", "note_index": 0}
        cmn = ChunkedMemoryNote("short text", note_data, model, max_chars=800)
        assert len(cmn.chunks) == 1
        assert cmn.chunks[0]["embedding"] is not None

    def test_multiple_chunks_long_text(self):
        model = FakeModel()
        long_text = "chunk one content. " * 200  # ~3000 chars
        note_data = {"text": long_text, "note_index": 1}
        cmn = ChunkedMemoryNote(long_text, note_data, model, max_chars=800)
        assert len(cmn.chunks) >= 3

    def test_max_similarity_returns_best(self):
        model = FakeModel()
        cmn = ChunkedMemoryNote("hello world", {"text": "test"}, model, max_chars=800)
        # use same text for query → should have high sim
        query_emb = model.encode("hello world")
        sim = cmn.max_similarity(query_emb)
        assert 0.0 <= sim <= 1.0

    def test_note_preserves_original(self):
        model = FakeModel()
        note_data = {"text": "original", "score": 0.8, "note_index": 5}
        cmn = ChunkedMemoryNote("original text", note_data, model)
        assert cmn.note["score"] == 0.8
        assert cmn.note["note_index"] == 5

    def test_empty_text_no_chunks(self):
        model = FakeModel()
        cmn = ChunkedMemoryNote("", {"text": ""}, model)
        assert len(cmn.chunks) == 0
        assert cmn.max_similarity(None) == 0.0
