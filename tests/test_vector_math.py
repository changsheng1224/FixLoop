"""Finite, stable similarity is shared across routing consumers."""

import math

import pytest

from agent_runtime.vector_math import cosine_similarity


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ([1, 0], [1, 0], 1.0),
        ([1, 0], [0, 1], 0.0),
        ([1, 2], [-1, -2], -1.0),
        ([1e308, 1e308], [1e308, 1e308], 1.0),
        ([1e-308, 1e-308], [1e-308, 1e-308], 1.0),
        ([], [], 0.0),
        ([0, 0], [1, 2], 0.0),
        ([1], [1, 2], 0.0),
        ([math.nan], [1], 0.0),
        ([math.inf], [1], 0.0),
        (None, [1], 0.0),
        ([[1, 2]], [1, 2], 0.0),
        ("12", [1, 2], 0.0),
    ],
)
def test_similarity(a, b, expected):
    assert cosine_similarity(a, b) == pytest.approx(expected)


def test_numpy_vectors_have_same_semantics():
    np = pytest.importorskip("numpy")
    assert cosine_similarity(np.array([1.0, 2.0]), [1, 2]) == pytest.approx(1.0)
    assert cosine_similarity(np.array([math.nan]), [1]) == 0.0
