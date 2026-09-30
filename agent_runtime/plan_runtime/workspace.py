"""Content versions and OS-held workspace leases, including on Windows."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from .models import digest

IGNORED = frozenset(
    {
        ".git",
        ".agent",
        ".pytest_cache",
        "__pycache__",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        ".pytest-tmp",
    }
)


def workspace_id(root: str) -> str:
    return digest(os.path.normcase(str(Path(root).resolve())))


def snapshot(root: str) -> dict[str, str]:
    base = Path(root).resolve()
    result = {}
    for directory, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("workspace_symlink_unverifiable")
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"workspace_symlink_unverifiable: {path}")
            result[path.relative_to(base).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def changes(before: dict, after: dict) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


@contextmanager
def workspace_lease(path: Path):
    """Nonblocking process lease. Crash releases it; PID guessing is unnecessary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ValueError("workspace_busy: previous controller not stopped") from exc
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
