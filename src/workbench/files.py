"""Handle-relative project I/O; never follow links or overwrite an unchecked inode."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from agent_runtime.linux_sandbox.guarded_fs import WindowsBroker, validate_path
from src.workbench.models import ConflictError

PRIVATE = ".fixloop-s0-private-"
EXCLUDED = frozenset(
    {
        ".git",
        ".fixloop",
        ".agent",
        ".agents",
        ".codex",
        ".claude",
        ".cursor",
        ".aws",
        ".ssh",
        ".gh-config",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
        "build",
    }
)
POLICY = "workbench-files-v1"
MAX_FILE = 32 * 1024 * 1024
MAX_TREE = 128 * 1024 * 1024
MAX_ENTRIES = 20_000


def excluded(name):
    lower = name.casefold()
    return lower in EXCLUDED or lower.startswith((".env", PRIVATE))


def managed(path):
    parts = validate_path(path)
    if not parts or any(excluded(part) for part in parts):
        raise ValueError("path is outside snapshot scope")
    return parts


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ProjectFiles:
    """Pinned root for a service lifetime. Windows and Linux native backends.

    External editors are not locked out globally. Reads and replacements validate
    preimages; an unexpected change stops the transaction and preserves backups.
    """

    def __init__(self, root):
        raw = Path(root).expanduser().absolute()
        # Reject aliases, including junctions/symlink ancestors, before resolving.
        for path in (raw, *raw.parents):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("project root and ancestors must not be links")
        self.root = raw.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("project root must be a directory")
        self.broker = WindowsBroker(self.root) if os.name == "nt" else None
        if self.broker:
            self.identity = self.broker.metadata(self.broker._check(self.broker.root_handle))[
                "identity"
            ]
            self.fd = None
        else:
            if not hasattr(os, "O_NOFOLLOW") or not hasattr(ctypes.CDLL(None), "renameat2"):
                raise ValueError("S3 native restoration supports Windows and Linux")
            self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self.fd)
            self.identity = [info.st_dev, info.st_ino]

    def close(self):
        if self.broker:
            self.broker.close()
        elif self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def check_root(self):
        if self.broker:
            self.broker._check(self.broker.root_handle)
        else:
            info = self.root.lstat()
            if stat.S_ISLNK(info.st_mode) or [info.st_dev, info.st_ino] != self.identity:
                raise ConflictError("project root identity changed")

    @contextmanager
    def parent(self, path):
        self.check_root()
        parts = managed(path)
        if self.broker:
            with self.broker.anchored(path) as pair:
                yield pair
            return
        handles = []
        try:
            fd = self.fd
            for name in parts[:-1]:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                handles.append(fd)
            yield fd, parts[-1]
            # An ancestor rename cannot redirect I/O out of the root. Detect any
            # detachment before claiming a successful operation.
            check = self.fd
            checks = []
            try:
                for name, pinned in zip(parts[:-1], handles, strict=True):
                    check = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=check
                    )
                    checks.append(check)
                    a, b = os.fstat(check), os.fstat(pinned)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise ConflictError("project ancestor identity changed")
            finally:
                for handle in checks:
                    os.close(handle)
            self.check_root()
        finally:
            for handle in reversed(handles):
                os.close(handle)

    def _win_read(self, handle, info):
        size = (info.size_high << 32) | info.size_low
        if size > MAX_FILE:
            raise ValueError("snapshot file exceeds 32 MiB")
        buffer = ctypes.create_string_buffer(65536)
        count = ctypes.c_uint32()
        chunks = []
        while size:
            self.broker._ok(
                self.broker.api.ReadFile(
                    handle, buffer, min(size, 65536), ctypes.byref(count), None
                )
            )
            if not count.value:
                raise ConflictError("file changed while reading")
            chunks.append(buffer.raw[: count.value])
            size -= count.value
        return b"".join(chunks)

    def _read_at(self, parent, name):
        if self.broker:
            with self.broker._relative(parent, name, 0x80000000, 0) as handle:
                info = self.broker._check(handle)
                if info.attributes & 0x10:
                    return {"kind": "directory", "mode": 0o755}, None
                data = self._win_read(handle, info)
                mode = 0o444 if info.attributes & 1 else 0o644
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if stat.S_ISDIR(info.st_mode):
                    return {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}, None
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("special files and hardlinks are outside snapshot scope")
                if info.st_size > MAX_FILE:
                    raise ValueError("snapshot file exceeds 32 MiB")
                chunks, size = [], 0
                while chunk := os.read(fd, 65536):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise ValueError("snapshot file exceeds 32 MiB")
                    chunks.append(chunk)
                data = b"".join(chunks)
                after = os.fstat(fd)
                if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ConflictError("file changed while reading")
                mode = stat.S_IMODE(info.st_mode)
            finally:
                os.close(fd)
        return {"kind": "file", "mode": mode, "size": len(data), "digest": digest(data)}, data

    def read(self, path):
        try:
            with self.parent(path) as (parent, name):
                return self._read_at(parent, name)
        except FileNotFoundError:
            return None, None

    def _names(self, path):
        if self.broker:
            with self.broker.open_path(path, directory=True) as (handle, _):
                return sorted(self.broker._names(handle))
        fd = self.fd if not path else None
        try:
            if path:
                with self.parent(path) as (parent, name):
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            return sorted(os.listdir(fd))
        finally:
            if path and fd is not None:
                os.close(fd)

    def scan(self, sink=None):
        self.check_root()
        entries, total = {}, 0

        def walk(directory):
            nonlocal total
            names = self._names(directory)
            # Reject case aliases even on Linux, so a snapshot is portable.
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("case ambiguous project entries")
            for name in names:
                if excluded(name):
                    continue
                path = directory + "/" + name if directory else name
                managed(path)
                entry, data = self.read(path)
                if entry is None:
                    raise ConflictError("project changed during snapshot")
                entries[path] = entry
                if len(entries) > MAX_ENTRIES:
                    raise ValueError("snapshot exceeds 20000 entries")
                if data is not None:
                    total += len(data)
                    if total > MAX_TREE:
                        raise ValueError("snapshot exceeds 128 MiB")
                    if sink:
                        sink(entry["digest"], data)
                else:
                    walk(path)

        walk("")
        self.check_root()
        return {"policy": POLICY, "root_identity": self.identity, "entries": entries}

    def preflight(self, before, after):
        # Protected children survive. A directory containing them cannot be
        # deleted or replaced with a file, even when absent from the manifest.
        for path, entry in before.items():
            target = after.get(path)
            if entry["kind"] == "directory" and (target is None or target["kind"] != "directory"):
                if any(excluded(name) for name in self._names(path)):
                    raise ConflictError(
                        "restore would remove a directory containing excluded files"
                    )

    def _rename(self, parent, source, target, destination=None):
        libc = ctypes.CDLL(None, use_errno=True)
        if (
            libc.renameat2(
                parent,
                os.fsencode(source),
                parent if destination is None else destination,
                os.fsencode(target),
                1,
            )
            != 0
        ):
            code = ctypes.get_errno()
            raise OSError(code, "conditional rename failed")

    def backup_name(self, operation, path):
        return PRIVATE + operation + "-" + digest(path.encode())[:20]

    def change(self, path, expected, target, data, operation):
        """Conditional change; source bytes remain in a deterministic backup.

        Missing entries between removing a source and publishing a target are
        recognized by the durable journal. Backups are cleaned only after commit.
        """
        with self.parent(path) as (parent, name):
            try:
                actual, _ = self._read_at(parent, name)
            except FileNotFoundError:
                actual = None
            if actual != expected:
                raise ConflictError("workspace changed after restore preview")
            if expected == target:
                return
            if self.broker:
                self._win_change(parent, name, path, expected, target, data, operation)
            else:
                self._posix_change(parent, name, path, expected, target, data, operation)

    def _win_mode(self, handle, mode):
        # FILE_BASIC_INFO: preserve timestamps (zero means unchanged).
        value = (ctypes.c_longlong * 5)()
        value[4] = 0x80 if mode & 0o200 else 1
        self.broker._ok(
            self.broker.api.SetFileInformationByHandle(
                handle, 0, ctypes.byref(value), ctypes.sizeof(value)
            )
        )

    def _win_change(self, parent, name, path, expected, target, data, operation):
        b = self.broker
        if expected:
            directory = expected["kind"] == "directory"
            with b._relative(parent, name, 0x80010000 | 0x100, 0, directory=directory) as source:
                info = b._check(source)
                if directory:
                    if target and target["kind"] == "directory":
                        return  # Windows directory permissions are not portable.
                    if list(b._names(source)):
                        raise ConflictError("directory acquired unexpected children")
                    b._disposition(source, True)
                else:
                    current = {
                        "kind": "file",
                        "mode": 0o444 if info.attributes & 1 else 0o644,
                        "size": (info.size_high << 32) | info.size_low,
                        "digest": digest(self._win_read(source, info)),
                    }
                    if current != expected:
                        raise ConflictError("source changed before replacement")
                    b._rename(source, b.root_handle, self.backup_name(operation, path))
        if target is None:
            return
        if target["kind"] == "directory":
            with b._relative(parent, name, 0x80000000, 3, create=True, directory=True) as handle:
                b._check(handle)
            return
        temp = PRIVATE + operation + "-stage"
        with b._relative(parent, temp, 0xC0010100, 0, create=True) as staging:
            b._disposition(staging, True)
            try:
                if b._info(staging).links != 0:
                    raise ConflictError("staging acquired a hardlink")
                count = ctypes.c_uint32()
                for offset in range(0, len(data), 65536):
                    chunk = data[offset : offset + 65536]
                    b._ok(b.api.WriteFile(staging, chunk, len(chunk), ctypes.byref(count), None))
                    if count.value != len(chunk):
                        raise OSError(errno.EIO, "short snapshot write")
                b._ok(b.api.FlushFileBuffers(staging))
                self._win_mode(staging, target["mode"])
                b._disposition(staging, False)
                b._rename(staging, parent, name)  # never replace a new editor file
            except BaseException:
                # A published target is retained for journal recovery.
                if b._canonical(staging).endswith(temp):
                    b._disposition(staging, True)
                raise

    def _posix_change(self, parent, name, path, expected, target, data, operation):
        if expected:
            if expected["kind"] == "directory":
                if target and target["kind"] == "directory":
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        os.fchmod(fd, target["mode"])
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    return
                os.rmdir(name, dir_fd=parent)  # cannot remove unexpected contents
            else:
                backup = self.backup_name(operation, path)
                self._rename(parent, name, backup, self.fd)
                # Validate the inode actually moved. A racing edit is preserved.
                fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
                try:
                    info = os.fstat(fd)
                    chunks = []
                    while chunk := os.read(fd, 65536):
                        chunks.append(chunk)
                        if sum(map(len, chunks)) > MAX_FILE:
                            raise ConflictError("moved source exceeds snapshot limit")
                    if (
                        info.st_nlink != 1
                        or digest(b"".join(chunks)) != expected["digest"]
                        or stat.S_IMODE(info.st_mode) != expected["mode"]
                    ):
                        raise ConflictError("source changed during replacement; backup retained")
                finally:
                    os.close(fd)
        if target:
            if target["kind"] == "directory":
                os.mkdir(name, 0o700, dir_fd=parent)
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fchmod(fd, target["mode"])
                    os.fsync(fd)
                finally:
                    os.close(fd)
            else:
                fd = os.open(".", os.O_RDWR | os.O_TMPFILE, 0o600, dir_fd=parent)
                try:
                    offset = 0
                    while offset < len(data):
                        count = os.write(fd, data[offset:])
                        if not count:
                            raise OSError(errno.EIO, "short snapshot write")
                        offset += count
                    os.fchmod(fd, target["mode"])
                    os.fsync(fd)
                    # No writes follow publishing; linkat refuses existing names.
                    libc = ctypes.CDLL(None, use_errno=True)
                    if (
                        libc.linkat(
                            -100,
                            os.fsencode(f"/proc/self/fd/{fd}"),
                            parent,
                            os.fsencode(name),
                            0x400,
                        )
                        != 0
                    ):
                        raise OSError(ctypes.get_errno(), "conditional publication failed")
                finally:
                    os.close(fd)
        os.fsync(parent)

    def cleanup(self, operation, path, expected):
        if not expected or expected["kind"] != "file":
            return
        try:
            self.check_root()
            with self._backup_root() as parent:
                backup = self.backup_name(operation, path)
                if self.broker:
                    b = self.broker
                    with b._relative(parent, backup, 0x80010100, 0) as handle:
                        info = b._info(handle)
                        # Private paths cannot pass the public path validator.
                        if (
                            info.attributes & 0x400
                            or info.links != 1
                            or digest(self._win_read(handle, info)) != expected["digest"]
                        ):
                            raise ConflictError("unexpected restore backup; retained")
                        if info.attributes & 1:
                            self._win_mode(handle, 0o644)
                        b._disposition(handle, True)
                else:
                    fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        chunks = []
                        while chunk := os.read(fd, 65536):
                            chunks.append(chunk)
                        if digest(b"".join(chunks)) != expected["digest"]:
                            raise ConflictError("unexpected restore backup; retained")
                    finally:
                        os.close(fd)
                    os.unlink(backup, dir_fd=parent)
                    os.fsync(parent)
        except FileNotFoundError:
            pass

    @contextmanager
    def _backup_root(self):
        yield self.broker.root_handle if self.broker else self.fd
