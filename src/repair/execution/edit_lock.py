"""文件锁定与未读不可写（Patcher Primary Phase A）。"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "EditLockState",
    "clear_active_edit_lock",
    "get_active_edit_lock",
    "normalize_repo_rel",
    "set_active_edit_lock",
]

_ACTIVE_LOCKS: dict[str, EditLockState] = {}


def _root_key(repo_root: str | Path) -> str:
    try:
        return str(Path(repo_root).resolve())
    except OSError:
        return str(repo_root)


def set_active_edit_lock(repo_root: str | Path, lock: EditLockState | None) -> None:
    key = _root_key(repo_root)
    if lock is None:
        _ACTIVE_LOCKS.pop(key, None)
    else:
        _ACTIVE_LOCKS[key] = lock


def get_active_edit_lock(repo_root: str | Path | None) -> EditLockState | None:
    if not repo_root:
        return None
    return _ACTIVE_LOCKS.get(_root_key(repo_root))


def clear_active_edit_lock(repo_root: str | Path) -> None:
    set_active_edit_lock(repo_root, None)


def normalize_repo_rel(path: str, repo_root: str | Path | None = None) -> str:
    """归一化为仓库相对 posix 路径；拒绝 ``..`` 逃逸时返回空串。"""
    raw = (path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        # 绝对路径：若在 root 下则相对化
        if repo_root and raw:
            try:
                rel = Path(raw).resolve().relative_to(Path(repo_root).resolve())
                return str(rel).replace("\\", "/")
            except (ValueError, OSError):
                return ""
        return raw.lstrip("/")
    parts = []
    for p in raw.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            return ""
        parts.append(p)
    return "/".join(parts)


class EditLockState:
    """维护 allowed_edit 与成功 Read 集合。"""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        allowed_edit: set[str] | frozenset[str] | list[str] | None = None,
        max_expand: int = 2,
        max_auto_allow: int = 5,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.allowed_edit: set[str] = {
            normalize_repo_rel(p, self.repo_root) for p in (allowed_edit or []) if p
        }
        self.allowed_edit.discard("")
        self.read_set: set[str] = set()
        self.unread_write_reject_count = 0
        self.apply_path_reject_count = 0
        self.apply_patch_ok_count = 0
        self.edit_lint_reject_count = 0
        self.max_expand = max(0, int(max_expand))
        self.expand_count = 0
        self.max_auto_allow = max(0, int(max_auto_allow))
        # 空种子时：禁止仅靠 read 自动灌锁，须先 expand_lock
        self.require_expand_before_auto = False
        # Phase B：每 turn 至多一次写
        self.write_serial = True
        self.write_done_this_turn = False

    def begin_turn(self) -> None:
        self.write_done_this_turn = False

    def mark_write_done(self) -> None:
        self.write_done_this_turn = True

    def seed_and_preread(self, paths: list[str] | set[str]) -> list[str]:
        """种子路径加入 allowed_edit，并对存在的文件预读进 read_set。"""
        seeded: list[str] = []
        for raw in paths or []:
            rel = normalize_repo_rel(raw, self.repo_root)
            if not rel:
                continue
            self.allowed_edit.add(rel)
            seeded.append(rel)
            self.mark_read(rel)
        return seeded

    def mark_read(self, path: str, *, auto_allow_impl: bool = False) -> bool:
        rel = normalize_repo_rel(path, self.repo_root)
        if not rel:
            return False
        target = self.repo_root / rel
        try:
            if not target.is_file():
                return False
        except OSError:
            return False
        self.read_set.add(rel)
        if auto_allow_impl and rel.endswith(".py") and not _is_test_path(rel):
            if self.require_expand_before_auto and self.expand_count <= 0:
                return True
            if rel not in self.allowed_edit and len(self.allowed_edit) < self.max_auto_allow:
                self.allowed_edit.add(rel)
        return True

    def expand_lock(self, path: str) -> tuple[bool, str]:
        """显式扩锁（默认最多 2 次）；扩后须再 read 才可写。"""
        rel = normalize_repo_rel(path, self.repo_root)
        if not rel:
            return False, "path_escape_or_invalid"
        if rel in self.allowed_edit:
            return True, "already_allowed"
        if self.expand_count >= self.max_expand:
            return False, f"expand_lock_max:{self.max_expand}"
        target = self.repo_root / rel
        try:
            if not target.is_file():
                return False, f"not_a_file:{rel}"
        except OSError as e:
            return False, f"stat_failed:{e}"
        self.allowed_edit.add(rel)
        self.expand_count += 1
        return True, f"expanded:{rel}"

    def check_write(self, path: str) -> tuple[bool, str]:
        rel = normalize_repo_rel(path, self.repo_root)
        if not rel:
            self.apply_path_reject_count += 1
            return False, "path_escape_or_invalid"
        if rel not in self.allowed_edit:
            self.apply_path_reject_count += 1
            return False, f"not_in_allowed_edit:{rel}"
        if rel not in self.read_set:
            self.unread_write_reject_count += 1
            return False, f"unread_before_write:{rel}"
        return True, ""

    def check_patch_paths(self, paths: list[str]) -> tuple[bool, list[str]]:
        bad: list[str] = []
        for p in paths or []:
            ok, _ = self.check_write(p)
            if not ok:
                rel = normalize_repo_rel(p, self.repo_root) or p
                bad.append(rel)
        return (not bad), bad


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return (
        "/tests/" in f"/{p}"
        or p.startswith("tests/")
        or p.startswith("test_")
        or p.endswith("_test.py")
        or p.endswith("/conftest.py")
    )
