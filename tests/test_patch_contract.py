"""Patch contracts assert resulting bytes, rejection and transaction boundaries."""

import pytest

from agent_runtime.patch_engine import PatchError, apply_text, parse_patch_input


@pytest.mark.parametrize(
    "source,diff",
    [
        ("actual\n", "@@ -1 +1 @@\n-stale\n+new\n"),
        ("actual\nold\n", "@@ -1,2 +1,2 @@\n stale\n-old\n+new\n"),
    ],
)
def test_stale_preimage_rejected(source, diff):
    with pytest.raises(PatchError, match="preimage mismatch"):
        apply_text(source, parse_patch_input({"diff": diff}))


def test_hunks_use_original_coordinates():
    diff = "@@ -1 +1,2 @@\n a\n+insert\n@@ -4 +5 @@\n-d\n+D\n"
    assert apply_text("a\nb\nc\nd\n", parse_patch_input({"diff": diff})) == "a\ninsert\nb\nc\nD\n"


def test_source_prefix_is_not_file_header():
    diff = "@@ -1 +1 @@\n---old\n+++new\n"
    assert apply_text("--old\n", parse_patch_input({"diff": diff})) == "++new\n"


@pytest.mark.parametrize("source,expected", [("old\n", "new\n"), ("old\r\n", "new\r\n")])
def test_line_endings(source, expected):
    assert apply_text(source, parse_patch_input({"diff": "@@ -1 +1 @@\n-old\n+new\n"})) == expected


def test_no_final_newline_marker():
    diff = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
    assert apply_text("old", parse_patch_input({"diff": diff})) == "new"


def test_empty_insertion():
    assert apply_text("", parse_patch_input({"diff": "@@ -0,0 +1 @@\n+new\n"})) == "new\n"


def test_unsupported_mode_metadata_suffix_is_rejected():
    with pytest.raises(PatchError, match="unsupported patch format"):
        parse_patch_input({"diff": "new file mode 100644extra\n--- /dev/null\n+++ b/a.py\n"})


def test_bad_counts_rejected():
    with pytest.raises(ValueError):
        parse_patch_input({"diff": "@@ -1,2 +1 @@\n-old\n+new\n"})
