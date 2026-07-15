"""patch_engine 单测。"""

from agent_runtime.patch_engine import (
    apply_plan,
    build_preview,
    format_preview_text,
    parse_patch_input,
    try_build_patch_preview,
)


class TestParsePatchInput:
    def test_legacy_mode(self):
        plan = parse_patch_input({"old_text": "a", "new_text": "b"})
        assert plan.mode == "legacy"
        assert plan.old_text == "a"

    def test_diff_mode(self):
        diff = "@@ -1,1 +1,1 @@\n-old\n+new\n"
        plan = parse_patch_input({"diff": diff})
        assert plan.mode == "diff"
        assert len(plan.hunks) == 1

    def test_missing_params(self):
        try:
            parse_patch_input({"path": "x.py"})
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestApplyPlan:
    def test_legacy_single_replace(self):
        plan = parse_patch_input({"old_text": "old", "new_text": "new"})
        assert apply_plan("old text", plan) == "new text"

    def test_legacy_zero_matches(self):
        plan = parse_patch_input({"old_text": "missing", "new_text": "x"})
        assert apply_plan("hello", plan) is None

    def test_multi_hunk_diff(self):
        text = "line1\nold\nline3\nfoo\n"
        diff = "@@ -1,3 +1,3 @@\n line1\n-old\n+new\n line3\n@@ -4,1 +4,2 @@\n foo\n+bar\n"
        plan = parse_patch_input({"diff": diff})
        result = apply_plan(text, plan)
        assert result == "line1\nnew\nline3\nfoo\nbar\n"


class TestPreview:
    def test_build_preview_counts(self):
        plan = parse_patch_input({"old_text": "a\nb", "new_text": "c"})
        preview = build_preview("f.py", plan)
        assert preview.hunk_count == 1
        assert preview.lines_removed == 2
        assert preview.lines_added == 1

    def test_format_preview_text(self):
        plan = parse_patch_input({"old_text": "x", "new_text": "y"})
        preview = build_preview("a.py", plan)
        text = format_preview_text(preview)
        assert "预览" in text
        assert "-x" in text
        assert "+y" in text

    def test_try_build_patch_preview_success(self):
        meta, err = try_build_patch_preview(
            "f.py", "hello", {"old_text": "hello", "new_text": "world"}
        )
        assert err is None
        assert meta["hunk_count"] == 1
        assert "preview_text" in meta

    def test_try_build_patch_preview_mismatch(self):
        meta, err = try_build_patch_preview("f.py", "hello", {"old_text": "zzz", "new_text": "x"})
        assert meta is None
        assert "0 次" in err


class TestPatchEquivalence:
    def test_full_match(self):
        from src.eval.patch_utils import patch_equivalence

        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert patch_equivalence(diff, diff) == "full"

    def test_none_no_common_files(self):
        from src.eval.patch_utils import patch_equivalence

        a = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        b = "--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert patch_equivalence(a, b) == "none"

    def test_none_empty(self):
        from src.eval.patch_utils import patch_equivalence

        assert patch_equivalence("", "") == "none"

    def test_partial_overlap(self):
        from src.eval.patch_utils import patch_equivalence

        a = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        b = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-older\n+newer\n--- a/y.py\n+++ b/y.py\n"
        assert patch_equivalence(a, b) == "partial"
