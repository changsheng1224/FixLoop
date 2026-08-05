"""verify 前 test_patch overlay 与 target 推断回归。"""

from __future__ import annotations

from pathlib import Path

from src.repair.verify_test_patch import (
    VerifyTestPatchOverlay,
    extract_targets_from_test_patch,
    iter_test_patch_paths,
)


def _sample_test_patch() -> str:
    return (
        "diff --git a/lib/matplotlib/tests/test_backend_ps.py "
        "b/lib/matplotlib/tests/test_backend_ps.py\n"
        "--- a/lib/matplotlib/tests/test_backend_ps.py\n"
        "+++ b/lib/matplotlib/tests/test_backend_ps.py\n"
        "@@ -1,2 +1,6 @@\n"
        " def test_placeholder():\n"
        "     assert True\n"
        "+\n"
        "+def test_empty_line():\n"
        "+    assert True\n"
    )


class TestTestPatchOverlay:
    def test_apply_and_restore(self, tmp_path: Path):
        repo = tmp_path / "repo"
        target = repo / "lib" / "matplotlib" / "tests" / "test_backend_ps.py"
        target.parent.mkdir(parents=True)
        original = "def test_placeholder():\n    assert True\n"
        target.write_text(original, encoding="utf-8")
        patch = _sample_test_patch()

        with VerifyTestPatchOverlay(repo, patch) as overlay:
            assert overlay.applied
            text = target.read_text(encoding="utf-8")
            assert "def test_empty_line" in text

        assert target.read_text(encoding="utf-8") == original

    def test_extract_targets_includes_new_test(self):
        targets = extract_targets_from_test_patch(_sample_test_patch())
        assert "lib/matplotlib/tests/test_backend_ps.py" in targets
        assert any(t.endswith("::test_empty_line") for t in targets)

    def test_iter_paths(self):
        assert iter_test_patch_paths(_sample_test_patch()) == [
            "lib/matplotlib/tests/test_backend_ps.py"
        ]
