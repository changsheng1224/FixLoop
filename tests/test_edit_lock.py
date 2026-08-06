"""Edit lock：allowed_edit + 未读不可写（Phase A）。"""

from __future__ import annotations

from pathlib import Path

from src.repair.execution.edit_lock import EditLockState


def test_unread_write_rejected(tmp_path: Path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    lock = EditLockState(repo_root=tmp_path, allowed_edit={"a.py"})
    ok, reason = lock.check_write("a.py")
    assert ok is False
    assert "read" in reason.lower() or "未读" in reason
    assert lock.unread_write_reject_count == 1


def test_after_read_write_allowed(tmp_path: Path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    lock = EditLockState(repo_root=tmp_path, allowed_edit={"a.py"})
    assert lock.mark_read("a.py") is True
    ok, reason = lock.check_write("a.py")
    assert ok is True
    assert reason == ""


def test_out_of_allowed_rejected(tmp_path: Path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y\n", encoding="utf-8")
    lock = EditLockState(repo_root=tmp_path, allowed_edit={"a.py"})
    lock.mark_read("b.py")
    ok, reason = lock.check_write("b.py")
    assert ok is False
    assert "allowed" in reason.lower() or "lock" in reason.lower() or "越" in reason


def test_path_safety_rejects_dotdot(tmp_path: Path):
    lock = EditLockState(repo_root=tmp_path, allowed_edit={"a.py"})
    ok, _ = lock.check_write("../outside.py")
    assert ok is False


def test_preread_seeds(tmp_path: Path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    lock = EditLockState(repo_root=tmp_path, allowed_edit=set())
    lock.seed_and_preread(["a.py"])
    assert "a.py" in lock.allowed_edit
    assert "a.py" in lock.read_set
    ok, _ = lock.check_write("a.py")
    assert ok is True


def test_check_patch_paths(tmp_path: Path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    lock = EditLockState(repo_root=tmp_path, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    ok, bad = lock.check_patch_paths(["a.py", "evil.py"])
    assert ok is False
    assert "evil.py" in bad


def test_require_expand_blocks_auto_allow_until_expand(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x=1\n", encoding="utf-8")
    lock = EditLockState(repo_root=tmp_path, allowed_edit=set(), max_auto_allow=5)
    lock.require_expand_before_auto = True
    assert lock.mark_read("pkg/a.py", auto_allow_impl=True)
    assert "pkg/a.py" not in lock.allowed_edit
    ok, _ = lock.expand_lock("pkg/a.py")
    assert ok
    assert "pkg/a.py" in lock.allowed_edit
