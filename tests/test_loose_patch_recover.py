"""Loose unified-diff 回收。"""

from __future__ import annotations

from src.repair.loose_patch_recover import (
    parse_patches_with_recover,
    recover_patches_from_text,
)
from src.repair.patch_applier import parse_patches


def test_recover_fenced_diff():
    text = """
Here is the fix:

```diff
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,3 +1,3 @@
 def do_work(x):
-    return x + 1
+    return x + 2
```
"""
    patches = recover_patches_from_text(text)
    assert len(patches) == 1
    assert patches[0].file_path == "pkg/mod.py"
    assert "return x + 2" in patches[0].diff
    assert patches[0].explanation == "loose_diff_recover"


def test_recover_bare_diff():
    text = """
--- a/foo.py
+++ b/foo.py
@@ -1 +1 @@
-old
+new
"""
    patches = recover_patches_from_text(text)
    assert len(patches) == 1
    assert patches[0].file_path == "foo.py"


def test_recover_empty_on_garbage():
    assert recover_patches_from_text("no patch here") == []
    assert recover_patches_from_text("") == []


def test_parse_patches_with_recover_prefers_json():
    answer = '[{"file_path": "a.py", "original_lines": "a", "patched_lines": "b"}]'
    patches = parse_patches_with_recover(answer)
    assert len(patches) == 1
    assert patches[0].file_path == "a.py"
    assert patches[0].explanation != "loose_diff_recover"


def test_parse_patches_json_miss_then_loose():
    text = """
```patch
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-a
+b
```
"""
    assert parse_patches(text) == []
    patches = parse_patches_with_recover(text)
    assert len(patches) == 1
    assert patches[0].file_path == "x.py"
