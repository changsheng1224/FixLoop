"""仓库文件快照：Orchestrator 回滚与 Baseline 变更检测共用。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SKIP_DIRS = frozenset({".agent", ".pytest_cache", "__pycache__", ".git"})


class RollbackUnconfirmedError(RuntimeError):
    """Restoration did not establish the original managed file state."""


@dataclass(frozen=True)
class FileImage:
    content: bytes
    mode: int
    mtime_ns: int
    link: str | None = None
    directory_link: bool = False


class RepoSnapshot(dict[str, str]):
    """Text projection for patch export, with lossless file images for rollback."""

    def __init__(self, images, *, include=None, skip_dirs=DEFAULT_SKIP_DIRS):
        super().__init__()
        self.images = images
        self.include = include
        self.skip_dirs = skip_dirs
        for rel, image in images.items():
            if image.link is None:
                try:
                    self[rel] = image.content.decode("utf-8")
                except UnicodeDecodeError:
                    pass  # Binary files belong to the transaction, not a text patch.

    def manages(self, rel):
        return not any(p in self.skip_dirs for p in Path(rel).parts) and (
            self.include is None or self.include(rel)
        )

    def expected_versions(self, current, *, ignored):
        """Project the transaction post-state into Plan's file hash scope."""
        expected = {p: h for p, h in current.items() if not self.manages(p)}
        for rel, image in self.images.items():
            if not any(p in ignored for p in Path(rel).parts):
                expected[rel] = hashlib.sha256(image.content).hexdigest()
        return expected


def _paths(root, skip_dirs):
    def fail(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if any(getattr(Path(directory) / d, "is_junction", lambda: False)() for d in dirs):
            raise ValueError("snapshot_junction_unsupported")
        links = [d for d in dirs if (Path(directory) / d).is_symlink()]
        dirs[:] = [d for d in dirs if d not in links]
        for name in files + links:
            yield Path(directory) / name


def snapshot_repo(
    repo_root: str | Path,
    *,
    include: Callable[[str], bool] | None = None,
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
    sandbox_context=None,
) -> dict[str, str]:
    """Capture bytes and metadata; unreadable managed files fail before execution."""
    if sandbox_context is not None:
        from agent_runtime.linux_sandbox.workspace_state import workspace_snapshot

        return workspace_snapshot(sandbox_context, include=include)
    root = Path(repo_root)
    images = {}
    if not root.is_dir():
        return RepoSnapshot(images, include=include, skip_dirs=skip_dirs)
    for path in _paths(root, skip_dirs):
        rel = path.relative_to(root).as_posix()
        if any(part in skip_dirs for part in Path(rel).parts):
            continue
        if include is not None and not include(rel):
            continue
        info = path.lstat()
        link = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
        if link is None and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"snapshot_file_type_unsupported: {rel}")
        images[rel] = FileImage(
            b"" if link is not None else path.read_bytes(),
            stat.S_IMODE(info.st_mode),
            info.st_mtime_ns,
            link,
            bool(getattr(info, "st_file_attributes", 0) & 0x10) if link is not None else False,
        )
    return RepoSnapshot(images, include=include, skip_dirs=skip_dirs)


def restore_repo_snapshot(
    repo_root: str | Path,
    snapshot: dict[str, str],
    *,
    clear_pycache: bool = True,
    sandbox_context=None,
) -> None:
    if sandbox_context is not None:
        from agent_runtime.linux_sandbox.workspace_state import restore_workspace

        restore_workspace(sandbox_context, snapshot)
        return
    if not isinstance(snapshot, RepoSnapshot):
        raise TypeError("rollback requires a lossless RepoSnapshot")
    root = Path(repo_root).resolve()
    # Enumerate without following links. Remove new files and changed links first.
    for path in list(_paths(root, snapshot.skip_dirs)):
        rel = path.relative_to(root).as_posix()
        if not snapshot.manages(rel):
            continue
        image = snapshot.images.get(rel)
        if image is None or path.is_symlink():
            if not path.is_symlink():
                path.chmod(stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
    for rel, image in snapshot.images.items():
        target = root / rel
        # An added parent symlink was removed above; never restore through one.
        if not target.parent.resolve().is_relative_to(root):
            raise ValueError(f"rollback_path_escape: {rel}")
        if target.is_dir():
            target.rmdir()  # Only empty directories may replace managed files.
        target.parent.mkdir(parents=True, exist_ok=True)
        if image.link is not None:
            target.symlink_to(image.link, target_is_directory=image.directory_link)
            os.utime(target, ns=(image.mtime_ns, image.mtime_ns), follow_symlinks=False)
        else:
            if target.exists():
                target.chmod(stat.S_IWRITE | stat.S_IREAD)
            target.write_bytes(image.content)
            target.chmod(image.mode)
            os.utime(target, ns=(image.mtime_ns, image.mtime_ns))
    restored = snapshot_repo(root, include=snapshot.include, skip_dirs=snapshot.skip_dirs)
    if restored.images != snapshot.images:
        raise RuntimeError("rollback_outcome_unconfirmed")
    if clear_pycache:
        clear_repo_pycache(repo_root)


def clear_repo_pycache(repo_root: str | Path) -> None:
    """删除 repo 内 __pycache__，避免 pytest 子进程读到过期 .pyc。"""
    root = Path(repo_root)
    if not root.is_dir():
        return
    for cache_dir in sorted(root.rglob("__pycache__"), reverse=True):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)


def repo_changed(
    repo_root: str | Path,
    before: dict[str, str],
    *,
    include: Callable[[str], bool] | None = None,
    sandbox_context=None,
) -> bool:
    current = snapshot_repo(repo_root, include=include, sandbox_context=sandbox_context)
    if sandbox_context is not None:
        return current.versions != before.versions
    if isinstance(before, RepoSnapshot):
        return current.images != before.images
    return current != before
