"""每任务 Git Worktree：隔离文件访问范围，支持取消/失败后回收。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """Worktree 创建 / 清理失败。"""


@dataclass
class WorktreeHandle:
    run_id: str
    repo_root: Path
    path: Path
    branch: str

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "repo_root": str(self.repo_root),
            "path": str(self.path),
            "branch": self.branch,
        }


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def worktree_base(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / ".agent" / "worktrees"


def default_worktree_path(repo_root: Path | str, run_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (run_id or "run"))[:80]
    return worktree_base(repo_root) / safe


def create_worktree(
    repo_root: Path | str,
    run_id: str,
    *,
    path: Path | None = None,
    base_ref: str = "HEAD",
) -> WorktreeHandle:
    """在 ``.agent/worktrees/<run_id>`` 创建独立 worktree。

    需要 *repo_root* 为 git 仓库。失败抛 ``WorktreeError``。
    """
    root = Path(repo_root).resolve()
    if not (root / ".git").exists() and not (root / ".git").is_file():
        # bare check: git rev-parse
        probe = _git(root, "rev-parse", "--is-inside-work-tree", check=False)
        if probe.returncode != 0 or "true" not in (probe.stdout or "").lower():
            raise WorktreeError(f"not a git repository: {root}")

    wt_path = path or default_worktree_path(root, run_id)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if wt_path.exists():
        remove_worktree(root, wt_path, force=True)

    branch = f"fixloop/wt-{run_id[:32]}"
    # 若分支已存在则删除
    _git(root, "branch", "-D", branch, check=False)
    add = _git(
        root,
        "worktree",
        "add",
        "-b",
        branch,
        str(wt_path),
        base_ref,
        check=False,
    )
    if add.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed: {(add.stderr or add.stdout or '').strip()}"
        )
    return WorktreeHandle(run_id=run_id, repo_root=root, path=wt_path.resolve(), branch=branch)


def remove_worktree(
    repo_root: Path | str,
    path: Path | str,
    *,
    force: bool = True,
) -> bool:
    """移除 worktree 目录；返回是否成功清理。"""
    root = Path(repo_root).resolve()
    wt = Path(path).resolve()
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(wt))
    result = _git(root, *args, check=False)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    _git(root, "worktree", "prune", check=False)
    return not wt.exists() or result.returncode == 0


def cleanup_stale_worktrees(
    repo_root: Path | str,
    *,
    max_age_hours: float = 24.0,
) -> int:
    """删除超过 *max_age_hours* 的 worktree 目录。返回删除数量。"""
    base = worktree_base(repo_root)
    if not base.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    root = Path(repo_root).resolve()
    for child in list(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            if remove_worktree(root, child, force=True):
                deleted += 1
    return deleted


def worktree_enabled() -> bool:
    flag = os.environ.get("FIXLOOP_USE_WORKTREE", "").strip().lower()
    return flag in ("1", "true", "yes", "on")
