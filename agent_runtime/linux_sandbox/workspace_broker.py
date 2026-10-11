"""Narrow native authority: stable reads and version-conditional inode replacement.

No code, subprocesses, parsing or tool semantics run here. File bytes cross the
private controller channel only. Existing file inodes are never written, so a
hardlink created concurrently cannot expose modified bytes outside the workspace.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import errno
import hashlib
import stat
import uuid
from dataclasses import dataclass, field

from .guarded_fs import MAX_IO, WindowsBroker, validate_path
from .workspace_contract import MAX_SOURCE_BYTES


@dataclass
class CommitOutcome:
    changed_files: list[str] = field(default_factory=list)
    recovery_files: dict[str, str] = field(default_factory=dict)
    error_code: str = ""


class GuardedWorkspaceBroker(WindowsBroker):
    """Per-call pinned read handles, retained until the isolated reader exits."""

    def __init__(self, root, *, hidden_dirs=()):
        super().__init__(root)
        self.hidden_dirs = frozenset(hidden_dirs)
        self._reads = contextlib.ExitStack()
        self._handles = {}

    def release_reads(self):
        self._reads.close()
        self._reads = contextlib.ExitStack()
        self._handles.clear()

    def close(self):
        try:
            if hasattr(self, "_reads"):
                self.release_reads()
        finally:
            super().close()

    def _pin_file_or_directory(self, path):
        if any(part in self.hidden_dirs for part in path.split("/")):
            raise PermissionError(errno.EACCES, "path excluded from command source view")
        if path in self._handles:
            return self._handles[path]
        if len(self._handles) >= 8192:
            raise OSError(errno.EMFILE, "bounded workspace read handles exceeded")
        # Native FILE_DIRECTORY_FILE can report several Windows error codes for
        # a regular file. First open without this constraint, then select a
        # pinned directory handle if necessary.
        with self.open_path(path) as (probe, info):
            directory = bool(info.attributes & 0x10)
        handle, _ = self._reads.enter_context(self.open_path(path, directory=directory))
        self._handles[path] = handle
        return handle

    def _read_bytes(self, handle, size, offset=0):
        if type(size) is not int or not 0 <= size <= MAX_IO:
            raise ValueError("invalid read size")
        if type(offset) is not int or not 0 <= offset <= (1 << 40):
            raise ValueError("invalid read offset")
        self._ok(self.api.SetFilePointerEx(handle, offset, None, 0))
        count = ctypes.c_uint32()
        buffer = ctypes.create_string_buffer(size)
        self._ok(self.api.ReadFile(handle, buffer, size, ctypes.byref(count), None))
        return buffer.raw[: count.value]

    def _digest(self, handle, info, *, max_bytes=MAX_SOURCE_BYTES):
        size = (info.size_high << 32) | info.size_low
        if info.attributes & 0x10 or size > max_bytes:
            raise OSError(errno.EFBIG, "bounded regular file required")
        digest, offset = hashlib.sha256(), 0
        while offset < size:
            data = self._read_bytes(handle, min(MAX_IO, size - offset), offset)
            if not data:
                raise OSError(errno.EIO, "incomplete pinned read")
            digest.update(data)
            offset += len(data)
        self._check(handle)
        return digest.hexdigest()

    def permission_versions(self, request):
        """Pin edit bases and offline inputs until the controller has stopped."""
        paths = dict(request.base_versions)
        if request.operation != "workspace":
            from .command_profile import install_inputs

            paths.update(dict.fromkeys(install_inputs(request)))
        versions = {}
        for path in paths:
            try:
                handle = self._pin_file_or_directory(path)
                versions[path] = self._digest(
                    handle, self._check(handle), max_bytes=256 * 1024 * 1024
                )
            except FileNotFoundError:
                if path not in request.base_versions or request.base_versions[path] is not None:
                    raise
                versions[path] = None
        return versions

    def call(self, request):
        if not isinstance(request, dict) or request.get("op") not in {"stat", "read", "list"}:
            raise PermissionError(errno.EPERM, "read-only workspace view")
        op, path = request["op"], request.get("path", "")
        handle = self._pin_file_or_directory(path)
        info = self._check(handle)
        if op == "stat":
            return self.metadata(info)
        if op == "read":
            if info.attributes & 0x10:
                raise IsADirectoryError(errno.EISDIR, "regular file required")
            data = self._read_bytes(handle, request["size"], request["offset"])
            self._check(handle)
            return base64.b64encode(data).decode("ascii")
        if not info.attributes & 0x10:
            raise NotADirectoryError(errno.ENOTDIR, "directory required")
        names = []
        for name in self._names(handle):
            if name in self.hidden_dirs:
                continue
            child = path + "/" + name if path else name
            try:
                validate_path(child)
                # Listing checks names but does not pin every file: searching a
                # large directory must not consume all read handles.
                if child in self._handles:
                    self._check(self._handles[child])
                    names.append(name)
                else:
                    with self.open_path(child) as (_, child_info):
                        if (
                            stat.S_ISREG(self.metadata(child_info)["mode"])
                            or child_info.attributes & 0x10
                        ):
                            names.append(name)
            except OSError:
                continue
            if len(names) >= 10000:
                raise OSError(errno.EFBIG, "bounded directory listing exceeded")
        return names

    def commit(self, request, reply, *, record, assert_owner=lambda: None):
        """Validate all bases, then replace entries without overwriting a racer.

        A multi-file edit is not a filesystem transaction. Each durable intent
        precedes renaming a checked original to a private recovery entry. The
        replacement uses no-replace rename; conflicts never clobber user edits.
        Failures report actual changes and any retained original recovery entry.
        """
        self.release_reads()
        outcome = CommitOutcome()
        if not reply.mutations:
            return outcome
        prepared = []
        with contextlib.ExitStack() as stack:
            # Missing parent directories are intentionally rejected: no hidden
            # directory mutations may happen during version validation.
            for path, expected in request.base_versions.items():
                parent, name = stack.enter_context(self.anchored(path))
                try:
                    source = stack.enter_context(self._relative(parent, name, 0x80010000, 0))
                    info = self._check(source)
                    observed = self._digest(source, info)
                except FileNotFoundError:
                    source, observed = None, None
                if observed != expected:
                    raise ValueError("stale_preimage: base version changed before commit")
                mutation = next((item for item in reply.mutations if item.path == path), None)
                if mutation is None:
                    continue
                staging = None
                if mutation.content is not None:
                    temporary = ".fixloop-private-stage-" + uuid.uuid4().hex
                    staging = stack.enter_context(
                        self._relative(parent, temporary, 0xC0010000, 0, create=True)
                    )
                    self._disposition(staging, True)
                    if self._info(staging).links != 0:
                        raise PermissionError(errno.EACCES, "staging alias denied")
                    data = mutation.content.encode("utf-8")
                    for offset in range(0, len(data), MAX_IO):
                        chunk, count = data[offset : offset + MAX_IO], ctypes.c_uint32()
                        self._ok(
                            self.api.WriteFile(
                                staging, chunk, len(chunk), ctypes.byref(count), None
                            )
                        )
                        if count.value != len(chunk):
                            raise OSError(errno.EIO, "short staged write")
                    self._ok(self.api.FlushFileBuffers(staging))
                prepared.append((path, parent, name, source, staging))
            assert_owner()
            for path, parent, name, source, staging in prepared:
                backup = ".fixloop-private-original-" + uuid.uuid4().hex
                recovery = path.rpartition("/")[0]
                recovery = recovery + "/" + backup if recovery else backup
                moved = False
                journal_failed = False
                try:
                    assert_owner()
                    record(
                        {
                            "path": path,
                            "recovery": recovery if source is not None else "",
                            "phase": "intent",
                        }
                    )
                    if source is not None:
                        self._check(source)
                        self._rename(source, parent, backup)
                        moved = True
                    if staging is not None:
                        self._disposition(staging, False)
                        try:
                            self._rename(staging, parent, name)  # never replace
                        except BaseException:
                            self._disposition(staging, True)
                            raise
                    outcome.changed_files.append(path)
                    try:
                        record({"path": path, "phase": "committed"})
                    except (OSError, ValueError):
                        journal_failed = True
                        raise
                    if source is not None:
                        self._disposition(source, True)
                except (OSError, ValueError) as exc:
                    if moved and path not in outcome.changed_files:
                        try:
                            self._rename(source, parent, name)  # never replace
                        except OSError:
                            outcome.recovery_files[path] = recovery
                            outcome.changed_files.append(path)
                    elif moved:
                        outcome.recovery_files[path] = recovery
                    outcome.error_code = (
                        "execution_uncertain"
                        if outcome.recovery_files or journal_failed
                        else "commit_failed"
                    )
                    try:
                        record(
                            {
                                "path": path,
                                "phase": "failed",
                                "error": str(exc).split(":", 1)[0],
                                "changed_files": outcome.changed_files,
                                "recovery_files": outcome.recovery_files,
                            }
                        )
                    except (OSError, ValueError):
                        outcome.error_code = "execution_uncertain"
                    break
        return outcome
