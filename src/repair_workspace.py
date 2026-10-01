"""CLI repository preparation and Git snapshots, independent of repair decisions."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from agent_runtime.cancellation import CancellationToken, CancelledError


class RepairInputError(ValueError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class RepoSource:
    location: str
    remote: bool


def parse_repo_source(value: str) -> RepoSource:
    value = value.strip()
    if not value:
        raise RepairInputError("input_invalid", "--repo 不能为空")
    path = Path(value).expanduser()
    if path.exists():
        if not path.is_dir():
            raise RepairInputError("input_invalid", "--repo 必须是目录或 GitHub 仓库地址")
        return RepoSource(str(path.resolve()), False)
    name = value
    if value.startswith("git@github.com:"):
        name = value.removeprefix("git@github.com:")
    elif "://" in value:
        url = urlsplit(value)
        if url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment:
            raise RepairInputError("input_invalid", "只支持无凭据的 GitHub HTTPS / SSH 地址")
        name = url.path.removeprefix("/").removesuffix("/")
    name = name.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", name):
        raise RepairInputError("input_invalid", f"--repo 不存在或不是 GitHub 仓库: {value}")
    if name.split("/")[1] in {".", ".."}:
        raise RepairInputError("input_invalid", "无效的 GitHub 仓库名称")
    transport = "ssh" if value.startswith("git@") else "https"
    location = (
        f"git@github.com:{name}.git" if transport == "ssh" else f"https://github.com/{name}.git"
    )
    return RepoSource(location, True)


def validate_ref(ref: str | None) -> None:
    if ref and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", "."))
    ):
        raise RepairInputError("input_invalid", "--ref 必须是分支、tag 或 commit SHA")


def git_run(
    repo: Path | None,
    *args: str,
    token: CancellationToken | None = None,
    timeout: float = 120,
    env_extra: dict[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """No shell; drain pipes while polling cancellation and a bounded deadline."""
    executable = shutil.which("git")
    if not executable:
        raise RepairInputError("configuration_failed", "未找到 Git，请先安装 Git")
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    env.update(env_extra or {})
    command = [executable, *args]
    if token and token.is_cancelled:
        raise CancelledError("user")
    process = subprocess.Popen(
        command,
        cwd=repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    started = time.monotonic()
    try:
        while True:
            if token and token.is_cancelled:
                raise CancelledError("user")
            if time.monotonic() - started >= timeout:
                raise RepairInputError("timeout", f"Git 操作超过 {timeout:g} 秒")
            try:
                stdout, stderr = process.communicate(input=stdin, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                stdin = None
    finally:
        if process.poll() is None:
            _kill_git_process(process)
            process.communicate()
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-1500:].strip()
        raise RepairInputError("repository_failed", f"Git {args[0]} 失败: {detail}")
    return result


def _kill_git_process(process) -> None:
    """Reap Git's SSH/credential children as well as Git itself on interruption."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def clone_repository(
    source: RepoSource, destination: Path, *, ref: str | None, token: CancellationToken
) -> str:
    """Create a fresh checkout; never clean or reset an existing user repository."""
    validate_ref(ref)
    if destination.exists():
        raise RepairInputError("input_invalid", f"工作目录已存在: {destination}")
    result = git_run(
        None,
        "clone",
        "--config",
        "core.autocrlf=false",
        "--no-checkout",
        "--",
        source.location,
        str(destination),
        token=token,
        timeout=300,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-1500:]
        auth = any(
            word in detail.lower()
            for word in (
                "authentication",
                "permission denied",
                "could not read username",
                "repository not found",
            )
        )
        category = "authentication_failed" if auth else "clone_failed"
        raise RepairInputError(category, f"克隆失败（请检查地址、Git 登录或代理）: {detail}")
    requested = ref or "HEAD"
    sha = ""
    for candidate in (requested, f"refs/remotes/origin/{requested}"):
        resolved = git_run(
            destination,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{candidate}^{{commit}}",
            token=token,
            check=False,
        )
        if resolved.returncode == 0:
            sha = resolved.stdout.decode().strip()
            break
    if not sha:
        fetched = git_run(destination, "fetch", "--", "origin", requested, token=token, check=False)
        if fetched.returncode:
            raise RepairInputError("reference_not_found", f"找不到版本: {requested}")
        sha = (
            git_run(destination, "rev-parse", "FETCH_HEAD^{commit}", token=token)
            .stdout.decode()
            .strip()
        )
    git_run(destination, "checkout", "--detach", sha, token=token)
    return sha


_OMIT_DIRS = frozenset(
    {
        ".git",
        ".agent",
        ".fixloop",
        ".pytest_cache",
        "__pycache__",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        ".gh-config",
    }
)


def deliverable_path(name: str) -> bool:
    parts = Path(name).parts
    return (
        not any(part in _OMIT_DIRS for part in parts)
        and not Path(name).name.endswith((".pyc", ".pyo"))
        and Path(name).name != ".env"
    )


class GitSnapshot:
    """Use a separate index to export final files, without staging user changes."""

    def __init__(self, repo: Path, output: Path, *, token: CancellationToken | None = None):
        self.repo = repo
        self.token = token
        self.index = output / "snapshot.index"
        self.env = {"GIT_INDEX_FILE": str(self.index)}
        self.tracked = self._files("--cached")
        self.preexisting = set(self._files("--others", "--exclude-standard"))
        head = git_run(repo, "rev-parse", "--verify", "HEAD", check=False, token=self.token)
        self.base_commit = head.stdout.decode().strip() if head.returncode == 0 else ""
        self.head_paths = set()
        if self.base_commit:
            self.head_paths = set(
                git_run(
                    repo, "ls-tree", "-r", "--name-only", "-z", self.base_commit, token=self.token
                )
                .stdout.decode()
                .split("\0")
            ) - {""}
            self.tracked = sorted(set(self.tracked) | self.head_paths)
        try:
            self.baseline = self._tree(self.tracked)
        except BaseException:
            self.close()
            raise
        finally:
            # Delivery must still inspect the remaining worktree after cancellation.
            self.token = None

    def _files(self, *args: str) -> list[str]:
        data = git_run(self.repo, "ls-files", "-z", *args, token=self.token).stdout
        return [name for name in data.decode("utf-8").split("\0") if name]

    def _tree(self, names: list[str]) -> str:
        self.index.unlink(missing_ok=True)
        git_run(self.repo, "read-tree", "--empty", env_extra=self.env, token=self.token)
        # Seed tracked blobs so deleted files remain valid Git pathspecs.
        if self.base_commit:
            git_run(self.repo, "read-tree", self.base_commit, env_extra=self.env, token=self.token)
        allowed = sorted(
            {
                name
                for name in names
                if deliverable_path(name)
                and (
                    name in self.head_paths
                    or (self.repo / name).exists()
                    or (self.repo / name).is_symlink()
                )
            }
        )
        if allowed:
            paths = b"\0".join(f":(literal){name}".encode() for name in allowed) + b"\0"
            git_run(
                self.repo,
                "add",
                "-A",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                env_extra=self.env,
                stdin=paths,
                token=self.token,
            )
        return (
            git_run(self.repo, "write-tree", env_extra=self.env, token=self.token)
            .stdout.decode()
            .strip()
        )

    def export(self) -> tuple[bytes, list[str]]:
        additions = set(self._files("--others", "--exclude-standard")) - self.preexisting
        names = set(self.tracked) | (set(self._files("--cached")) - self.preexisting) | additions
        final = self._tree(list(names))
        patch = git_run(
            self.repo,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            self.baseline,
            final,
        ).stdout
        changed = (
            git_run(
                self.repo,
                "diff",
                "--name-only",
                "-z",
                self.baseline,
                final,
            )
            .stdout.decode()
            .split("\0")
        )
        return patch, [name for name in changed if name]

    def close(self) -> None:
        self.index.unlink(missing_ok=True)
        self.index.with_suffix(".index.lock").unlink(missing_ok=True)
