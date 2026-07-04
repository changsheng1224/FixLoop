"""仓库文件快照：Orchestrator 回滚与 Baseline 变更检测共用。"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

DEFAULT_SKIP_DIRS = frozenset({".agent", ".pytest_cache", "__pycache__", ".git"})


def snapshot_repo(
    repo_root: str | Path,
    *,
    include: Callable[[str], bool] | None = None,
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
) -> dict[str, str]:
    """读取 repo 文本快照。include(rel_path) 为 None 时包含除 skip_dirs 外全部文件。"""
    root = Path(repo_root)
    snap: dict[str, str] = {}
    if not root.is_dir():
        return snap
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in skip_dirs for part in Path(rel).parts):
            continue
        if include is not None and not include(rel):
            continue
        try:
            snap[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return snap


def restore_repo_snapshot(
    repo_root: str | Path,
    snapshot: dict[str, str],
    *,
    clear_pycache: bool = True,
) -> None:
    root = Path(repo_root)
    for rel, content in snapshot.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
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
) -> bool:
    return snapshot_repo(repo_root, include=include) != before
