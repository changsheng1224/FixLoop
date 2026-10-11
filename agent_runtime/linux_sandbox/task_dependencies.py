"""Native WSL task dependencies; no host mounts or controller state in bwrap."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .workspace_inventory import content_version

TASK_BASE = Path("/home/fixloop/tasks")


def task_key(request, environment: dict) -> str:
    return content_version(
        {
            "workspace": request.workspace_id,
            "task": request.task_id,
            "run": request.run_id,
            "runtime": environment["runtime_digest"],
        }
    )


@contextmanager
def task_environment(request, environment: dict, *, retiring=False):
    import fcntl

    # All parent directories belong to the dedicated user and are outside the
    # tool mount. Reject aliases before opening the lock or creating children.
    uid = os.getuid()
    for path in (TASK_BASE.parent, TASK_BASE):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError("sandbox_task_environment_invalid: unsafe task directory")
    task = TASK_BASE / task_key(request, environment)
    admission = os.open(TASK_BASE / "admission", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(admission, fcntl.LOCK_EX)
        directories = [path for path in TASK_BASE.iterdir() if path.is_dir()]
        if not task.exists() and (
            len(directories) >= 4096
            or (
                not retiring
                and sum(not (path / "retired.json").exists() for path in directories) >= 128
            )
        ):
            raise ValueError("resource_limit_exceeded: retained task budget")
        task.mkdir(mode=0o700, exist_ok=True)
        info = task.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError("sandbox_task_environment_invalid: unsafe task directory")
    finally:
        os.close(admission)
    descriptor = os.open(task / "lease", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_nlink != 1:
            raise ValueError("sandbox_task_environment_invalid: task lease alias")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("workspace_busy: task dependency lease") from exc
        if (task / "retired.json").exists() and not retiring:
            raise ValueError("sandbox_task_retired")
        if retiring:
            from .receipts import _write

            _write(task / "retired.json", {"key": task.name, "state": "retiring"})
            yield None
            return
        deps = task / "deps"
        deps.mkdir(mode=0o700, exist_ok=True)
        if deps.is_symlink() or not deps.is_dir():
            raise ValueError("sandbox_task_environment_invalid: dependency alias")
        yield deps
    finally:
        os.close(descriptor)


def dependency_version(root: Path) -> str:
    versions, total = {}, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("sandbox_task_environment_invalid: dependency directory alias")
        for name in files:
            path = Path(directory) / name
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                total += info.st_size
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or total > 256 * 1024 * 1024
                    or len(versions) >= 8192
                ):
                    raise ValueError("sandbox_task_environment_invalid: dependency budget or alias")
                digest = hashlib.sha256()
                read = 0
                while chunk := stream.read(65536):
                    read += len(chunk)
                    if read > info.st_size:
                        raise ValueError("sandbox_task_environment_invalid: dependency file grew")
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError("sandbox_task_environment_invalid: dependency changed")
                versions[path.relative_to(root).as_posix()] = digest.hexdigest()
    return content_version(versions)
