"""Tests for intent embedding index (mock embed_fn; no model load)."""

from __future__ import annotations

from agent_runtime.intent.embed_index import EmbedIndex, load_prototypes


def _one_hot(dim: int, idx: int) -> list[float]:
    v = [0.0] * dim
    v[idx] = 1.0
    return v


class TestEmbedIndex:
    def test_load_prototypes_has_actions(self):
        proto = load_prototypes()
        assert "ask" in proto
        assert "remember" in proto
        assert "repair_request" in proto
        assert len(proto["ask"]) >= 3

    def test_match_with_mock_embed(self):
        # Map known phrases to axes: 0=ask, 1=remember, 2=repair
        table = {
            "what is this": _one_hot(3, 0),
            "remember pytest": _one_hot(3, 1),
            "fix the bug": _one_hot(3, 2),
            "query ask": _one_hot(3, 0),
            "query remember": _one_hot(3, 1),
        }
        prototypes = {
            "ask": ["what is this"],
            "remember": ["remember pytest"],
            "repair_request": ["fix the bug"],
        }

        def embed_fn(text: str):
            return table.get(text, _one_hot(3, 0))

        idx = EmbedIndex(prototypes, embed_fn=embed_fn)
        m = idx.match("query remember")
        assert m is not None
        assert m.primary == "remember"
        assert m.score == 1.0
        assert m.margin == 1.0  # vs ask/repair at 0

    def test_margin_between_top2(self):
        prototypes = {
            "ask": ["a"],
            "remember": ["b"],
        }

        def embed_fn(text: str):
            if text == "a":
                return [1.0, 0.0]
            if text == "b":
                return [0.0, 1.0]
            # query closer to ask
            return [0.8, 0.2]

        idx = EmbedIndex(prototypes, embed_fn=embed_fn)
        m = idx.match("q")
        assert m is not None
        assert m.primary == "ask"
        assert m.top2 == "remember"
        assert m.margin > 0

    def test_unavailable_embed_returns_none(self):
        idx = EmbedIndex(embed_fn=None)
        assert idx.match("anything") is None

    def test_embed_fn_error_returns_none(self):
        def boom(_text: str):
            raise RuntimeError("no model")

        idx = EmbedIndex({"ask": ["x"]}, embed_fn=boom)
        assert idx.match("x") is None
