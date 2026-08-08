"""每任务 Git Worktree：隔离文件访问范围，支持取消/失败后回收。"""

from __future__ import annotations

import json
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


def _safe_run_id(run_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (run_id or "run"))[:80]


def _lease_path(repo_root: Path | str, run_id: str) -> Path:
    return worktree_base(repo_root) / f".{_safe_run_id(run_id)}.lease"


def _assert_worktree_path(repo_root: Path, path: Path) -> Path:
    base = worktree_base(repo_root).resolve()
    candidate = (base / path if not path.is_absolute() else path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise WorktreeError(f"worktree path outside managed base: {candidate}") from exc
    return candidate


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

    base = worktree_base(root)
    base.mkdir(parents=True, exist_ok=True)
    wt_path = _assert_worktree_path(root, path or default_worktree_path(root, run_id))
    lease = _lease_path(root, run_id)
    if lease.exists():
        try:
            payload = json.loads(lease.read_text(encoding="utf-8"))
            if time.time() - float(payload.get("heartbeat", payload.get("created_at", 0))) < 24 * 3600:
                raise WorktreeError(f"worktree lease already active: {run_id}")
        except WorktreeError:
            raise
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        lease.unlink(missing_ok=True)
    try:
        with lease.open("x", encoding="utf-8") as handle:
            json.dump(
                {"run_id": run_id, "created_at": time.time(), "heartbeat": time.time()},
                handle,
            )
    except FileExistsError as exc:
        raise WorktreeError(f"worktree lease already active: {run_id}") from exc
    except OSError as exc:
        raise WorktreeError(f"cannot acquire worktree lease: {run_id}") from exc
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if wt_path.exists():
        try:
            remove_worktree(root, wt_path, force=True)
        except WorktreeError:
            lease.unlink(missing_ok=True)
            raise

    branch = f"fixloop/wt-{_safe_run_id(run_id)[:32]}"
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
        lease.unlink(missing_ok=True)
        raise WorktreeError(
            f"git worktree add failed: {(add.stderr or add.stdout or '').strip()}"
        )
    return WorktreeHandle(run_id=run_id, repo_root=root, path=wt_path, branch=branch)


def refresh_worktree_lease(handle: WorktreeHandle) -> None:
    """Refresh the managed lease; called by long-running repair loops."""
    payload = {"run_id": handle.run_id, "heartbeat": time.time()}
    _lease_path(handle.repo_root, handle.run_id).write_text(json.dumps(payload), encoding="utf-8")


def remove_worktree(
    repo_root: Path | str,
    path: Path | str,
    *,
    force: bool = True,
) -> bool:
    """移除 worktree 目录；返回是否成功清理。"""
    root = Path(repo_root).resolve()
    wt = _assert_worktree_path(root, Path(path))
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(wt))
    lease_run_id = wt.name
    lease_meta = wt / ".fixloop-worktree-lease.json"
    if lease_meta.is_file():
        try:
            lease_run_id = str(json.loads(lease_meta.read_text(encoding="utf-8")).get("run_id") or wt.name)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    result = _git(root, *args, check=False)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    _git(root, "worktree", "prune", check=False)
    _lease_path(root, lease_run_id).unlink(missing_ok=True)
    lease_meta.unlink(missing_ok=True)
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
        if child.name.startswith(".") or child.name.endswith(".lease"):
            continue
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
