"""Phase B：expand_lock / apply_patch / compact / quick_test 单测。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.repair.execution.edit_lock import EditLockState


def test_expand_lock_adds_path_and_requires_read():
    raw = tempfile.mkdtemp(prefix="fixloop-b1-")
    root = Path(raw)
    (root / "a.py").write_text("x\n", encoding="utf-8")
    (root / "b.py").write_text("y\n", encoding="utf-8")
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    ok, reason = lock.expand_lock("b.py")
    assert ok is True
    assert "b.py" in lock.allowed_edit
    assert lock.expand_count == 1
    # expand 后未读仍不可写
    w_ok, w_reason = lock.check_write("b.py")
    assert w_ok is False
    assert "unread" in w_reason
    lock.mark_read("b.py")
    assert lock.check_write("b.py")[0] is True


def test_expand_lock_max_twice():
    raw = tempfile.mkdtemp(prefix="fixloop-b1b-")
    root = Path(raw)
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (root / name).write_text("x\n", encoding="utf-8")
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"}, max_expand=2)
    assert lock.expand_lock("b.py")[0]
    assert lock.expand_lock("c.py")[0]
    ok, reason = lock.expand_lock("d.py")
    assert ok is False
    assert "max" in reason.lower() or "2" in reason


def test_mark_read_impl_auto_allows_up_to_n():
    raw = tempfile.mkdtemp(prefix="fixloop-b1c-")
    root = Path(raw)
    for i in range(6):
        (root / f"m{i}.py").write_text("x\n", encoding="utf-8")
    lock = EditLockState(repo_root=root, allowed_edit=set(), max_auto_allow=5)
    for i in range(6):
        lock.mark_read(f"m{i}.py", auto_allow_impl=True)
    assert len(lock.allowed_edit) == 5
    assert "m5.py" not in lock.allowed_edit
