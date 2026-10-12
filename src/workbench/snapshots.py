"""Immutable, content addressed binary snapshots outside the project root."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

from agent_runtime.state_root import native_io_path
from src.workbench.files import MAX_ENTRIES, MAX_FILE, MAX_TREE, POLICY, digest, managed
from src.workbench.models import ConflictError

_HASH = re.compile(r"^[a-f0-9]{64}$")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def replace_durable(source, target):
    if os.name == "nt":
        import ctypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        api.MoveFileExW.restype = ctypes.c_int
        if not api.MoveFileExW(str(native_io_path(source)), str(native_io_path(target)), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
        sync_directory(target.parent)


class SnapshotRepository:
    def __init__(self, root):
        self.root = Path(root)
        for path in (self.root, self.root / "blobs", self.root / "trees"):
            path.mkdir(parents=True, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("snapshot storage must not contain directory aliases")

    def _read(self, path, limit):
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("snapshot storage alias denied")
        if info.st_size > limit:
            raise ValueError("snapshot storage limit exceeded")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("snapshot storage limit exceeded")
        return data

    def _put(self, path, data):
        if path.exists():
            if self._read(path, len(data)) != data:
                raise ValueError("corrupt content addressed snapshot")
            return
        temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            replace_durable(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def blob(self, key, data=None):
        if not isinstance(key, str) or not _HASH.fullmatch(key):
            raise ValueError("invalid blob key")
        path = self.root / "blobs" / key
        if data is not None:
            if len(data) > MAX_FILE or digest(data) != key:
                raise ValueError("invalid snapshot blob")
            self._put(path, data)
        value = self._read(path, MAX_FILE)
        if digest(value) != key:
            raise ValueError("snapshot blob hash mismatch")
        return value

    def validate(self, manifest):
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"policy", "root_identity", "entries"}
            or manifest["policy"] != POLICY
        ):
            raise ValueError("unsupported snapshot manifest")
        identity = manifest["root_identity"]
        if (
            not isinstance(identity, list)
            or len(identity) not in (2, 3)
            or any(type(x) is not int or x < 0 for x in identity)
        ):
            raise ValueError("invalid root identity")
        entries = manifest["entries"]
        if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
            raise ValueError("invalid snapshot entry count")
        total, paths = 0, set()
        for path, entry in entries.items():
            parts = managed(path)
            if path.casefold() in paths:
                raise ValueError("case ambiguous snapshot")
            paths.add(path.casefold())
            if (
                not isinstance(entry, dict)
                or type(entry.get("mode")) is not int
                or not 0 <= entry["mode"] <= 0o777
            ):
                raise ValueError("invalid snapshot mode")
            if entry.get("kind") == "directory":
                if set(entry) != {"kind", "mode"}:
                    raise ValueError("invalid directory manifest")
            elif entry.get("kind") == "file":
                if (
                    set(entry) != {"kind", "mode", "size", "digest"}
                    or type(entry["size"]) is not int
                    or not 0 <= entry["size"] <= MAX_FILE
                ):
                    raise ValueError("invalid file manifest")
                data = self.blob(entry["digest"])
                if len(data) != entry["size"]:
                    raise ValueError("snapshot size mismatch")
                total += len(data)
                if total > MAX_TREE:
                    raise ValueError("snapshot tree limit exceeded")
            else:
                raise ValueError("invalid snapshot entry kind")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                if entries.get(parent, {}).get("kind") != "directory":
                    raise ValueError("snapshot parent directory missing")
        return manifest

    def save(self, files, *, root_identity=None):
        manifest = files.scan(self.blob)
        if files.scan() != manifest:
            raise ConflictError("project changed during snapshot; retry")
        if root_identity is not None:
            manifest = {**manifest, "root_identity": root_identity}
        self.validate(manifest)
        key = "tree-" + digest(canonical(manifest))
        self._put(self.root / "trees" / key, canonical(manifest))
        return key, manifest

    def load(self, key, expected=None):
        if not isinstance(key, str) or not key.startswith("tree-") or not _HASH.fullmatch(key[5:]):
            raise ValueError("invalid tree key")
        data = self._read(self.root / "trees" / key, 8 * 1024 * 1024)
        if digest(data) != key[5:]:
            raise ValueError("snapshot manifest hash mismatch")
        manifest = self.validate(json.loads(data))
        if canonical(manifest) != data:
            raise ValueError("noncanonical snapshot manifest")
        if expected is not None:
            expected = {k: v for k, v in expected.items() if k != "restore_operation"}
            if expected != manifest:
                raise ValueError("stored snapshot metadata mismatch")
        return manifest
