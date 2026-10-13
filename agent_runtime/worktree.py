"""Trusted Git administration; ownership records live outside the checkout."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_runtime.state_root import (
    managed_state_base,
    managed_storage_root,
    project_id,
    workspace_id,
)


class WorktreeError(RuntimeError):
    """Worktree creation or ownership-safe cleanup failed."""


@dataclass
class WorktreeHandle:
    run_id: str
    repo_root: Path
    path: Path
    branch: str
    state_base: Path | None = None  # Shared storage root, before project partitioning.
    ownership: str = "task"
    retention: str = "preserve"
    directory_identity: tuple[int, int] = ()

    def as_dict(self) -> dict:
        values = {
            key: str(value)
            if isinstance(value, Path)
            else list(value)
            if isinstance(value, tuple)
            else value
            for key, value in asdict(self).items()
        }
        return {**values, "project_id": self.project_id, "workspace_id": self.workspace_id}

    @property
    def project_id(self) -> str:
        return project_id(self.repo_root)

    @property
    def workspace_id(self) -> str:
        return workspace_id(self.path)


def git_administration_options(cwd: Path) -> tuple[dict[str, str], list[str]]:
    # Never inherit alternate indexes/worktrees or project hooks for administration.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    # Read only scalar newline settings; `git config` does not run filter drivers.
    config = subprocess.run(
        [
            "git",
            "config",
            "--null",
            "--get-regexp",
            r"^(core\.(autocrlf|eol)|filter\..*\.(clean|smudge|process|required))$",
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if config.returncode not in (0, 1):
        raise WorktreeError("git configuration unavailable")
    overrides = {
        "core.hooksPath": os.devnull,
        "core.fsmonitor": "false",
        "submodule.recurse": "false",
    }
    for entry in config.stdout.split("\x00"):
        if not entry:
            continue
        key, value = entry.split("\n", 1)
        if key == "core.autocrlf":
            if value.lower() not in {"true", "false", "input"}:
                raise WorktreeError("unsupported git newline configuration")
            overrides[key] = value.lower()
        elif key == "core.eol":
            if value.lower() not in {"lf", "crlf", "native"}:
                raise WorktreeError("unsupported git newline configuration")
            overrides[key] = value.lower()
        elif re.fullmatch(r"filter\..*\.(clean|smudge|process|required)", key):
            overrides[key] = "false" if key.endswith(".required") else ""
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    options = [item for key, value in overrides.items() for item in ("-c", f"{key}={value}")]
    return env, options


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env, options = git_administration_options(cwd)
    return subprocess.run(
        ["git", *options, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def _safe_run_id(run_id: str) -> str:
    from agent_runtime.linux_sandbox.task_context import checked_id

    try:
        return checked_id(run_id)
    except ValueError as exc:
        raise WorktreeError("invalid worktree run_id") from exc


def worktree_base(repo_root: Path | str, state_base: Path | str | None = None) -> Path:
    return managed_state_base(repo_root, state_base) / "worktrees"


def default_worktree_path(repo_root: Path | str, run_id: str, *, state_base=None) -> Path:
    return worktree_base(repo_root, state_base) / _safe_run_id(run_id)


def _lease_path(repo_root: Path | str, run_id: str, state_base=None) -> Path:
    return (
        managed_state_base(repo_root, state_base)
        / "worktree-leases"
        / f"{_safe_run_id(run_id)}.json"
    )


def _assert_worktree_path(repo_root: Path, path: Path, state_base=None) -> Path:
    base = worktree_base(repo_root, state_base).resolve()
    candidate = (base / path if not path.is_absolute() else path).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise WorktreeError(f"worktree path outside managed base: {candidate}")
    if candidate == repo_root or candidate.is_relative_to(repo_root):
        raise WorktreeError("worktree inside source repository")
    return candidate


def create_worktree(
    repo_root: Path | str,
    run_id: str,
    *,
    path: Path | None = None,
    base_ref: str = "HEAD",
    state_base=None,
) -> WorktreeHandle:
    """Create from a clean source. Never replace a directory, branch or old lease.

    A dirty source must be used directly (or supplied as an existing worktree),
    rather than silently dropping its modifications when branching from HEAD.
    """
    root = Path(repo_root).resolve()
    run_id = _safe_run_id(run_id)
    if not base_ref or base_ref.startswith("-") or "\x00" in base_ref:
        raise WorktreeError("invalid worktree base_ref")
    base = worktree_base(root, state_base)
    base.mkdir(parents=True, exist_ok=True)
    wt = _assert_worktree_path(
        root, path or default_worktree_path(root, run_id, state_base=state_base), state_base
    )
    branch = f"fixloop/wt-{run_id}"
    try:
        probe = _git(root, "rev-parse", "--is-inside-work-tree", check=False)
        if probe.returncode or probe.stdout.strip() != "true":
            raise WorktreeError(f"not a git repository: {root}")
        top = _git(root, "rev-parse", "--show-toplevel")
        if Path(top.stdout.strip()).resolve() != root:
            raise WorktreeError("worktree source must be the repository root")
        dirty = _git(root, "status", "--porcelain", "--untracked-files=all", check=False)
        if dirty.returncode:
            raise WorktreeError("worktree source status unavailable")
        if dirty.stdout:
            raise WorktreeError("worktree source has user changes; use the existing workspace")
        exists = _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
        if exists.returncode == 0 or wt.exists():
            raise WorktreeError("worktree path or branch already exists")
        lease = _lease_path(root, run_id, state_base)
        lease.parent.mkdir(parents=True, exist_ok=True)
        handle = WorktreeHandle(run_id, root, wt, branch, managed_storage_root(root, state_base))
        with lease.open("x", encoding="utf-8") as stream:
            json.dump({**handle.as_dict(), "status": "creating", "heartbeat": time.time()}, stream)
        add = _git(root, "worktree", "add", "-b", branch, str(wt), base_ref, check=False)
        if add.returncode:
            # Retain ambiguous partial creation for inspection, never force cleanup.
            if not wt.exists():
                lease.unlink(missing_ok=True)
            raise WorktreeError(f"git worktree add failed: {(add.stderr or add.stdout).strip()}")
        stat = wt.stat()
        handle.directory_identity = (stat.st_dev, stat.st_ino)
        _write_record(lease, {**handle.as_dict(), "status": "active", "heartbeat": time.time()})
        return handle
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"worktree creation failed: {exc}") from exc


def _owned_record(handle: WorktreeHandle) -> tuple[Path, dict]:
    lease = _lease_path(handle.repo_root, handle.run_id, handle.state_base)
    try:
        raw = json.loads(lease.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise WorktreeError("worktree ownership unavailable")
        for key in (
            "project_id",
            "workspace_id",
            "run_id",
            "repo_root",
            "path",
            "branch",
            "state_base",
            "ownership",
        ):
            if raw.get(key) != handle.as_dict()[key]:
                raise WorktreeError("worktree ownership mismatch")
        stat = handle.path.stat()
        if raw.get("directory_identity") != list(
            handle.directory_identity
        ) or handle.directory_identity != (stat.st_dev, stat.st_ino):
            raise WorktreeError("worktree directory identity changed")
        return lease, raw
    except (OSError, ValueError, TypeError) as exc:
        raise WorktreeError("worktree ownership unavailable") from exc


def refresh_worktree_lease(handle: WorktreeHandle) -> None:
    lease, raw = _owned_record(handle)
    raw.update(status="active", heartbeat=time.time())
    _write_record(lease, raw)


def _write_record(path: Path, raw: dict) -> None:
    import uuid

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump(raw, stream)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def retain_worktree(handle: WorktreeHandle, *, reason: str = "task_ended") -> None:
    if handle.ownership != "task":
        return
    lease, raw = _owned_record(handle)
    raw.update(status="retained", reason=reason, heartbeat=time.time(), retention="preserve")
    _write_record(lease, raw)


def adopt_worktree(
    repo_root: Path | str,
    path: Path | str,
    run_id: str,
    *,
    state_base=None,
    ownership: str = "user",
    branch: str = "",
) -> WorktreeHandle:
    """Validate a retained task checkout or a user-owned existing worktree."""
    root, wt = Path(repo_root).resolve(strict=True), Path(path).resolve(strict=True)
    try:
        common = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
        original = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if Path(common.stdout.strip()).resolve() != Path(original.stdout.strip()).resolve():
            raise WorktreeError("worktree belongs to another repository")
        actual = _git(wt, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
        if branch and actual != branch:
            raise WorktreeError("worktree branch changed")
        handle = WorktreeHandle(
            _safe_run_id(run_id),
            root,
            wt,
            actual,
            managed_storage_root(root, state_base),
            ownership,
        )
        stat = wt.stat()
        handle.directory_identity = (stat.st_dev, stat.st_ino)
        if ownership == "task":
            _assert_worktree_path(root, wt, state_base)
            _owned_record(handle)
        elif ownership != "user":
            raise WorktreeError("invalid worktree ownership")
        return handle
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError("worktree identity unavailable") from exc


def remove_worktree(
    repo_root: Path | str, path: Path | str, *, force: bool = False, state_base=None
) -> bool:
    """Remove only an owned checkout. Dirty data requires explicit force."""
    root = Path(repo_root).resolve()
    wt = _assert_worktree_path(root, Path(path), state_base)
    records = managed_state_base(root, state_base) / "worktree-leases"
    matches = []
    for record in records.glob("*.json"):
        try:
            raw = json.loads(record.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            if raw.get("path") == str(wt) and raw.get("repo_root") == str(root):
                matches.append(raw)
        except (OSError, ValueError, TypeError):
            continue
    if len(matches) != 1:
        raise WorktreeError("worktree ownership unavailable")
    raw = matches[0]
    handle = adopt_worktree(
        root, wt, raw["run_id"], state_base=state_base, ownership="task", branch=raw["branch"]
    )
    lease, _ = _owned_record(handle)
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(wt)]
    result = _git(root, *args, check=False)
    if result.returncode or wt.exists():
        return False  # Never rmtree as a fallback to a failed Git operation.
    lease.unlink()
    return True


def cleanup_stale_worktrees(
    repo_root: Path | str, *, max_age_hours: float = 24.0, state_base=None
) -> int:
    """Only explicitly disposable, clean, inactive task worktrees may be removed."""
    records = managed_state_base(repo_root, state_base) / "worktree-leases"
    deleted = 0
    for record in records.glob("*.json"):
        try:
            raw = json.loads(record.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            if (
                raw.get("retention") == "discard_if_clean"
                and raw.get("status") == "released"
                and float(raw["heartbeat"]) < time.time() - max_age_hours * 3600
                and remove_worktree(repo_root, raw["path"], state_base=state_base)
            ):
                deleted += 1
        except (WorktreeError, OSError, ValueError, KeyError, TypeError):
            continue
    return deleted


def worktree_enabled() -> bool:
    return os.environ.get("FIXLOOP_USE_WORKTREE", "").strip().lower() in ("1", "true", "yes", "on")
