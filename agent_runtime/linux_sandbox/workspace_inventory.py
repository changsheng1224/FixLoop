"""Bounded content inventory and text snapshot pages inside the guarded view."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .workspace_contract import MAX_SOURCE_BYTES, relative_path

SKIP_DIRS = frozenset(
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
MAX_FILES = 4096
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def content_version(versions: dict) -> str:
    return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    versions, total = {}, 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("policy_denied: inventory directory alias")
        for name in sorted(files):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            relative_path(rel)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("policy_denied: inventory requires regular single-link files")
            total += info.st_size
            if len(versions) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise ValueError("snapshot_limit_exceeded: inventory budget")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                read = 0
                while chunk := stream.read(65536):
                    read += len(chunk)
                    if read > info.st_size:
                        raise ValueError("stale_precondition: inventory file grew")
                    digest.update(chunk)
            after = path.stat()
            if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("stale_precondition: inventory changed during read")
            versions[rel] = digest.hexdigest()
    return versions


def snapshot_page(root: Path, args: dict) -> dict:
    cursor = args.get("cursor", "")
    if cursor:
        relative_path(cursor)
    versions = inventory(root)
    version = content_version(versions)
    if args.get("version", version) != version:
        raise ValueError("stale_precondition: snapshot content changed")
    files, used, next_cursor = {}, 0, ""
    for path in sorted(p for p in versions if p > cursor):
        target = root / path
        if target.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("snapshot_limit_exceeded: source exceeds snapshot size")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != versions[path]:
            raise ValueError("stale_precondition: snapshot page changed")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            next_cursor = path
            continue  # Binary versions remain in the whole-workspace inventory.
        size = len(json.dumps({path: text}, ensure_ascii=True).encode())
        if size > 600000:
            raise ValueError("snapshot_limit_exceeded: encoded source exceeds page size")
        if files and (used + size > 600000 or len(files) >= 128):
            return {"files": files, "version": version, "cursor": next_cursor, "complete": False}
        files[path] = text
        used += size
        next_cursor = path
    return {"files": files, "version": version, "cursor": next_cursor, "complete": True}
