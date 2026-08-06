"""仓库准备：clone + checkout base_commit。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.benchmark.swebench.types import SweInstance


class RepoPrepError(RuntimeError):
    """checkout 失败 → env。"""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _git_text(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args, check=False)
    return (proc.stdout or "").strip()


def github_clone_url(repo: str) -> str:
    """``owner/name`` → HTTPS clone URL。"""
    repo = repo.strip().removesuffix(".git")
    if repo.startswith("http://") or repo.startswith("https://") or repo.startswith("git@"):
        return repo
    return f"https://github.com/{repo}.git"


def preflight_repo(repo_path: Path | str, *, base_commit: str) -> dict:
    """校验运行前基线：HEAD、工作树、未跟踪文件、换行配置。"""

    repo = Path(repo_path)
    report: dict[str, object] = {
        "ok": False,
        "repo_path": str(repo.resolve()),
        "base_commit": base_commit,
        "head": "",
        "git_status": "",
        "line_endings": {},
        "reasons": [],
    }

    if not (repo / ".git").exists():
        report["reasons"] = ["missing_git_dir"]
        return report

    head = _git_text(repo, "rev-parse", "HEAD")
    report["head"] = head
    if not head:
        report["reasons"] = ["head_unavailable"]
        return report

    reasons: list[str] = []
    if head != base_commit:
        reasons.append("head_mismatch")

    status = _git_text(repo, "status", "--porcelain=v1", "--untracked-files=all")
    report["git_status"] = status
    if status:
        reasons.append("dirty_worktree")

    line_endings = {}
    for key in ("core.autocrlf", "core.eol", "core.safecrlf", "core.filemode"):
        line_endings[key] = _git_text(repo, "config", "--get", key)
    report["line_endings"] = line_endings

    report["ok"] = not reasons
    report["reasons"] = reasons
    return report


def prepare_repo(
    instance: SweInstance,
    work_root: Path | str,
    *,
    depth: int | None = None,
) -> Path:
    """在 ``work_root/<instance_id>`` 准备仓库并 checkout ``base_commit``。

    已存在则 fetch + checkout；失败抛 ``RepoPrepError``。
    默认使用 partial clone（``--filter=blob:none``）以降低首次拉取成本。
    """
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / instance.instance_id.replace("/", "__")
    url = github_clone_url(instance.repo)

    if not (dest / ".git").exists() and not dest.exists():
        cmd = ["git", "clone", "--filter=blob:none"]
        if depth:
            cmd.extend(["--depth", str(depth)])
        cmd.extend([url, str(dest)])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RepoPrepError(
                f"git clone failed for {instance.repo}: {(proc.stderr or proc.stdout or '')[:500]}"
            )
    elif not dest.exists():
        raise RepoPrepError(f"path exists but is not a git repo: {dest}")

    # 拉取目标 commit（partial clone 下按需取对象）
    _git(dest, "fetch", "--all", check=False)
    _git(dest, "fetch", "origin", instance.base_commit, check=False)

    co = _git(dest, "checkout", "--force", instance.base_commit, check=False)
    if co.returncode != 0:
        raise RepoPrepError(
            f"checkout {instance.base_commit} failed: {(co.stderr or co.stdout or '')[:500]}"
        )
    _git(dest, "clean", "-fdx", check=False)
    return dest.resolve()
