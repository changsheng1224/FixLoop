"""Experimental S0 direct-file broker and private FUSE view; no production gate.

The untrusted process never receives DrvFS or the broker channel. Windows opens
the actual files with exclusive handles, rejects reparse points/multiple links,
and pins parent directories against replacement during every operation.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import errno
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import uuid
from pathlib import Path

PROTECTED = {".git", ".agent", ".env"}
MAX_IO = 65536


def validate_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or len(path) > 4096:
        raise PermissionError(errno.EACCES, "invalid workspace path")
    if path == "":
        return ()
    parts = path.split("/")
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    for part in parts:
        if (
            not part
            or part in (".", "..")
            or part[-1:] in (".", " ")
            or any(c in part for c in '\\:*?"<>|')
            or any(ord(c) < 32 for c in part)
            or part.casefold() in PROTECTED
            or part.casefold().startswith(".fixloop-s0-private-")
            or part.split(".")[0].casefold() in reserved
        ):
            raise PermissionError(errno.EACCES, "ambiguous or protected workspace path")
    return tuple(parts)


class WindowsBroker:
    """Native Windows authority for a single root; never runs a shell."""

    def __init__(self, root: Path):
        from ctypes import wintypes as w

        if os.name != "nt":
            raise RuntimeError("Windows broker requires native Windows")
        self.root = root.resolve(strict=True)
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            w.DWORD,
            w.LPVOID,
            w.DWORD,
            w.DWORD,
            w.HANDLE,
        ]
        self.api.CreateFileW.restype = w.HANDLE
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.GetFileInformationByHandle.argtypes = [w.HANDLE, w.LPVOID]
        self.api.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
        self.api.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, w.LPVOID, w.LPVOID]
        self.api.WriteFile.argtypes = [w.HANDLE, w.LPCVOID, w.DWORD, w.LPVOID, w.LPVOID]
        self.api.SetFilePointerEx.argtypes = [w.HANDLE, ctypes.c_longlong, w.LPVOID, w.DWORD]
        self.api.SetEndOfFile.argtypes = [w.HANDLE]
        self.api.FlushFileBuffers.argtypes = [w.HANDLE]
        self.api.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        self.api.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        self.nt = ctypes.WinDLL("ntdll", use_last_error=True)
        self.nt.NtCreateFile.argtypes = [
            w.LPVOID,
            w.DWORD,
            w.LPVOID,
            w.LPVOID,
            w.LPVOID,
            w.DWORD,
            w.DWORD,
            w.DWORD,
            w.DWORD,
            w.LPVOID,
            w.DWORD,
        ]
        self.nt.NtCreateFile.restype = ctypes.c_long
        self.nt.NtSetInformationFile.argtypes = [
            w.HANDLE,
            w.LPVOID,
            w.LPVOID,
            w.DWORD,
            ctypes.c_int,
        ]
        self.nt.NtSetInformationFile.restype = ctypes.c_long
        self.nt.RtlNtStatusToDosError.argtypes = [w.DWORD]
        self.nt.RtlNtStatusToDosError.restype = w.DWORD

        class Info(ctypes.Structure):
            _fields_ = [
                ("attributes", w.DWORD),
                ("creation", w.FILETIME),
                ("access", w.FILETIME),
                ("modified", w.FILETIME),
                ("volume", w.DWORD),
                ("size_high", w.DWORD),
                ("size_low", w.DWORD),
                ("links", w.DWORD),
                ("index_high", w.DWORD),
                ("index_low", w.DWORD),
            ]

        self.info_type = Info
        with self._handle(self.root, 0, 3, 3) as handle:
            info = self._info(handle)
            if info.attributes & 0x400 or not info.attributes & 0x10:
                raise PermissionError(errno.EACCES, "invalid workspace root")
            self.canonical_root = self._canonical(handle)
        self.root_handle = self.api.CreateFileW(
            str(self.root), 0x80000000, 3, None, 3, 0x02200000, None
        )
        if self.root_handle == ctypes.c_void_p(-1).value:
            self.root_handle = None
            raise self._error()
        try:
            self._check(self.root_handle)
        except BaseException:
            self.close()
            raise

    def close(self):
        if getattr(self, "root_handle", None) is not None:
            self.api.CloseHandle(self.root_handle)
            self.root_handle = None

    def __del__(self):
        self.close()

    def _nt_ok(self, status):
        if status < 0:
            ctypes.set_last_error(self.nt.RtlNtStatusToDosError(status & 0xFFFFFFFF))
            raise self._error()

    @contextlib.contextmanager
    def _relative(self, parent, name, access, share, *, create=False, directory=False):
        """Resolve one component relative to a pinned native directory object."""
        from ctypes import wintypes as w

        class UnicodeString(ctypes.Structure):
            _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", w.LPWSTR)]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("length", w.ULONG),
                ("root", w.HANDLE),
                ("name", ctypes.POINTER(UnicodeString)),
                ("attributes", w.ULONG),
                ("security", w.LPVOID),
                ("qos", w.LPVOID),
            ]

        class IoStatus(ctypes.Structure):
            _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

        buffer = ctypes.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        text = UnicodeString(length, length + 2, ctypes.cast(buffer, w.LPWSTR))
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes), parent, ctypes.pointer(text), 0x40, None, None
        )
        handle, status = w.HANDLE(), IoStatus()
        self._nt_ok(
            self.nt.NtCreateFile(
                ctypes.byref(handle),
                access | 0x100000,
                ctypes.byref(attributes),
                ctypes.byref(status),
                None,
                0x80,
                share,
                2 if create else 1,
                0x00204020 | (1 if directory else 0),
                None,
                0,
            )
        )
        try:
            yield handle.value
        finally:
            self.api.CloseHandle(handle)

    @contextlib.contextmanager
    def anchored(self, path):
        parts = validate_path(path)
        with contextlib.ExitStack() as stack:
            parent = self.root_handle
            self._check(parent)
            for part in parts[:-1]:
                parent = stack.enter_context(
                    self._relative(parent, part, 0x80000000, 3, directory=True)
                )
                self._check(parent)
            yield parent, parts[-1] if parts else None

    def _error(self):
        code = ctypes.get_last_error()
        mapped = {
            2: errno.ENOENT,
            3: errno.ENOENT,
            5: errno.EACCES,
            32: errno.EACCES,
            80: errno.EEXIST,
            183: errno.EEXIST,
            145: errno.ENOTEMPTY,
            112: errno.ENOSPC,
            87: errno.EINVAL,
        }
        return OSError(mapped.get(code, errno.EIO), f"Windows file operation denied ({code})")

    def _ok(self, value):
        if not value:
            raise self._error()

    @contextlib.contextmanager
    def _handle(self, path, access, share, disposition):
        handle = self.api.CreateFileW(str(path), access, share, None, disposition, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise self._error()
        try:
            yield handle
        finally:
            self.api.CloseHandle(handle)

    def _info(self, handle):
        info = self.info_type()
        self._ok(self.api.GetFileInformationByHandle(handle, ctypes.byref(info)))
        return info

    def _canonical(self, handle):
        buffer = ctypes.create_unicode_buffer(32768)
        size = self.api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        self._ok(size and size < len(buffer))
        return buffer.value

    def _check(self, handle):
        info = self._info(handle)
        if info.attributes & 0x400:
            raise PermissionError(errno.EACCES, "reparse point denied")
        name = self._canonical(handle)
        if name.casefold() != self.canonical_root.casefold():
            prefix = self.canonical_root.rstrip("\\") + "\\"
            if not name.casefold().startswith(prefix.casefold()):
                raise PermissionError(errno.EACCES, "file outside workspace")
            validate_path(name[len(prefix) :].replace("\\", "/"))
        if not info.attributes & 0x10 and info.links != 1:
            raise PermissionError(errno.EACCES, "multiply linked file denied")
        return info

    @contextlib.contextmanager
    def parents(self, path):
        with self.anchored(path):
            yield self.root.joinpath(*validate_path(path))

    @contextlib.contextmanager
    def open_path(self, path, *, write=False, create=False, delete=False, directory=False):
        if not path and (write or create or delete):
            raise PermissionError(errno.EACCES, "root mutation denied")
        with self.anchored(path) as (parent, name):
            access = 0xC0000000 if write else 0x80000000
            access |= 0x10000 if delete else 0
            if name is None:
                yield parent, self._check(parent)
                return
            with self._relative(
                parent, name, access, 3 if directory else 0, create=create, directory=directory
            ) as handle:
                info = self._check(handle)
                yield handle, info

    def _names(self, handle):
        buffer = ctypes.create_string_buffer(65536)
        first = True
        while True:
            if not self.api.GetFileInformationByHandleEx(
                handle, 11 if first else 10, buffer, len(buffer)
            ):
                if ctypes.get_last_error() == 18:
                    return
                raise self._error()
            first = False
            offset = 0
            while True:
                next_offset = struct.unpack_from("<I", buffer.raw, offset)[0]
                length = struct.unpack_from("<I", buffer.raw, offset + 60)[0]
                name = buffer.raw[offset + 104 : offset + 104 + length].decode("utf-16-le")
                if name not in (".", ".."):
                    yield name
                if not next_offset:
                    break
                offset += next_offset

    def metadata(self, info):
        def seconds(t):
            return max(0, ((t.dwHighDateTime << 32) | t.dwLowDateTime) // 10000000 - 11644473600)

        directory = bool(info.attributes & 0x10)
        return {
            "mode": (stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o600),
            "size": (info.size_high << 32) | info.size_low,
            "nlink": 1,
            "mtime": seconds(info.modified),
            "atime": seconds(info.access),
        }

    def _disposition(self, handle, pending):
        value = ctypes.c_ubyte(pending)
        self._ok(self.api.SetFileInformationByHandle(handle, 4, ctypes.byref(value), 1))

    def _rename(self, handle, parent, name, *, replace=False):
        encoded = name.encode("utf-16-le")

        class RenameInfo(ctypes.Structure):
            _fields_ = [
                ("replace", ctypes.c_ubyte),
                ("root", ctypes.c_void_p),
                ("length", ctypes.c_uint32),
                ("name", ctypes.c_ushort * (len(encoded) // 2 + 1)),
            ]

        rename = RenameInfo()
        rename.replace = replace
        rename.root = parent
        rename.length = len(encoded)
        ctypes.memmove(ctypes.addressof(rename) + RenameInfo.name.offset, encoded, len(encoded))
        status = (ctypes.c_size_t * 2)()
        self._nt_ok(
            self.nt.NtSetInformationFile(
                handle, ctypes.byref(status), ctypes.byref(rename), ctypes.sizeof(rename), 10
            )
        )

    def _cow(self, path, request):
        """Write a new inode, never the checked source inode.

        Share modes/oplocks do NOT prevent CreateHardLink. Set delete-pending
        on the private staging file before checking nlink and before any writes:
        Windows then denies new hardlinks. Clear it only after all data writes
        finish, and atomically replace the allowed workspace directory entry.
        A concurrent link to the source retains the original bytes.
        """
        op = request["op"]
        offset = request["size"] if op == "truncate" else request["offset"]
        if type(offset) is not int or not 0 <= offset <= (1 << 28):
            raise ValueError("invalid file offset (S0 file limit: 256 MiB)")
        data = base64.b64decode(request["data"], validate=True) if op == "write" else b""
        if len(data) > MAX_IO:
            raise ValueError("write too large")
        if offset + len(data) > (1 << 28):
            raise OSError(errno.EFBIG, "S0 file limit: 256 MiB")
        if not path:
            raise PermissionError(errno.EACCES, "root mutation denied")
        with self.anchored(path) as (parent, name):
            temp = ".fixloop-s0-private-" + uuid.uuid4().hex
            with self._relative(parent, temp, 0xC0010000, 0, create=True) as staging:
                self._disposition(staging, True)
                try:
                    # Windows excludes this delete-pending directory entry from
                    # NumberOfLinks. Any remaining link is an unexpected alias.
                    if self._info(staging).links != 0:
                        raise PermissionError(errno.EACCES, "staging file acquired another link")
                    with self.open_path(path, write=True) as (source, info):
                        if info.attributes & 0x10:
                            raise IsADirectoryError(errno.EISDIR, "regular file required")
                        size = (info.size_high << 32) | info.size_low
                        if size > (1 << 28):
                            raise OSError(errno.EFBIG, "S0 file limit: 256 MiB")
                        buffer = ctypes.create_string_buffer(MAX_IO)
                        count = ctypes.c_uint32()
                        copied = 0
                        while copied < size:
                            self._ok(
                                self.api.ReadFile(
                                    source,
                                    buffer,
                                    min(MAX_IO, size - copied),
                                    ctypes.byref(count),
                                    None,
                                )
                            )
                            if not count.value:
                                raise OSError(errno.EIO, "source changed during copy")
                            read = count.value
                            self._ok(
                                self.api.WriteFile(staging, buffer, read, ctypes.byref(count), None)
                            )
                            if count.value != read:
                                raise OSError(errno.EIO, "short staging write")
                            copied += read
                    self._ok(self.api.SetFilePointerEx(staging, offset, None, 0))
                    if op == "truncate":
                        self._ok(self.api.SetEndOfFile(staging))
                    else:
                        self._ok(
                            self.api.WriteFile(staging, data, len(data), ctypes.byref(count), None)
                        )
                        if count.value != len(data):
                            raise OSError(errno.EIO, "short staging write")
                    self._ok(self.api.FlushFileBuffers(staging))
                    # No WriteFile/SetEndOfFile is permitted after clearing this.
                    self._disposition(staging, False)
                    self._rename(staging, parent, name, replace=True)
                except BaseException:
                    self._disposition(staging, True)
                    raise
        return len(data) if op == "write" else None

    def call(self, request):
        op, path = request["op"], request.get("path", "")
        validate_path(path)
        if op == "list":
            with self.open_path(path, directory=True) as (handle, info):
                if not info.attributes & 0x10:
                    raise NotADirectoryError(errno.ENOTDIR, "not a directory")
                names = []
                for name in self._names(handle):
                    relative = path + "/" + name if path else name
                    try:
                        # No metadata caching; aliases/links must be checked again on use.
                        self.call({"op": "stat", "path": relative})
                        names.append(name)
                    except OSError:
                        continue
                return names
        if op == "mkdir":
            if not path:
                raise PermissionError(errno.EACCES, "root mutation denied")
            with self.anchored(path) as (parent, name):
                with self._relative(
                    parent, name, 0x80000000, 3, create=True, directory=True
                ) as handle:
                    self._check(handle)
            return None
        if op not in {
            "stat",
            "read",
            "write",
            "create",
            "truncate",
            "unlink",
            "rmdir",
            "rename",
            "sync",
        }:
            raise PermissionError(errno.EPERM, "unsupported file operation")
        if op in {"write", "truncate"}:
            return self._cow(path, request)
        if op == "rename":
            destination = request["destination"]
            if not destination:
                raise PermissionError(errno.EACCES, "root mutation denied")
            validate_path(destination)
        with self.open_path(
            path,
            write=op in {"create", "sync"},
            create=op == "create",
            delete=op in {"unlink", "rmdir", "rename"},
        ) as (handle, info):
            if op == "stat":
                return self.metadata(info)
            if op in {"read", "sync"} and info.attributes & 0x10:
                raise IsADirectoryError(errno.EISDIR, "regular file required")
            if op == "create":
                return self.metadata(info)
            if op == "read":
                offset = request["offset"]
                if type(offset) is not int or not 0 <= offset <= (1 << 40):
                    raise ValueError("invalid file offset")
                self._ok(self.api.SetFilePointerEx(handle, offset, None, 0))
                count = ctypes.c_uint32()
                size = request["size"]
                if type(size) is not int or not 0 <= size <= MAX_IO:
                    raise ValueError("invalid read size")
                buffer = ctypes.create_string_buffer(size)
                self._ok(self.api.ReadFile(handle, buffer, size, ctypes.byref(count), None))
                return base64.b64encode(buffer.raw[: count.value]).decode("ascii")
            if op == "sync":
                self._ok(self.api.FlushFileBuffers(handle))
                return None
            if op in {"unlink", "rmdir"}:
                if bool(info.attributes & 0x10) != (op == "rmdir"):
                    raise OSError(
                        errno.EISDIR if info.attributes & 0x10 else errno.ENOTDIR,
                        "wrong entry type",
                    )
                self._disposition(handle, True)
                return None
            if op == "rename":
                with self.anchored(destination) as (parent, name):
                    # No replace: overwriting an unchecked target could touch protected
                    # state. Native rename is relative to the pinned directory.
                    self._rename(handle, parent, name)
                return None


class RemoteBroker:
    def __init__(self):
        self.lock = threading.Lock()

    def call(self, request):
        with self.lock:
            sys.stdout.write(json.dumps({"rpc": request}, ensure_ascii=True) + "\n")
            sys.stdout.flush()
            line = sys.stdin.readline(200000)
            if not line or not line.endswith("\n"):
                raise OSError(errno.EIO, "broker disconnected")
            response = json.loads(line)
            if "errno" in response:
                raise OSError(response["errno"], "broker denied operation")
            return response["result"]


def mount_fs(source, target, kind, options=None):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    if libc.mount(
        source.encode(),
        os.fsencode(target),
        kind.encode(),
        6,
        options.encode() if options else None,
    ):
        raise OSError(ctypes.get_errno(), "private mount failed")


def unmount(target):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.umount2(os.fsencode(target), 2):
        raise OSError(ctypes.get_errno(), "private unmount failed")


@contextlib.contextmanager
def private_binfmt():
    """Requires the worker's fresh user/mount namespace, never the distro global mount."""
    if (
        os.getuid() != 0
        or Path("/proc/self/ns/user").readlink() == Path("/proc/1/ns/user").readlink()
    ):
        raise RuntimeError("private binfmt requires a new mapped user namespace")
    with tempfile.TemporaryDirectory(prefix="fixloop-s0-binfmt-") as directory:
        mount_fs("binfmt_misc", directory, "binfmt_misc")
        try:
            path = Path(directory)
            if sorted(p.name for p in path.iterdir()) != ["register", "status"]:
                raise RuntimeError("binfmt instance is not private/empty")
            (path / "status").write_text("0")
            if (path / "status").read_text().strip() != "disabled":
                raise RuntimeError("binfmt disable did not apply")
            yield
        finally:
            unmount(directory)


class FuseView:
    """Small unprivileged FUSE 7.23 server for the diagnostic's fixed file profile.

    Zero entry/attribute lifetime and direct I/O prevent stale permission/data
    caches. Links, symlinks, xattrs, executable modes and replacement renames are
    unsupported and denied. Every data operation opens a fresh checked handle.
    """

    def __init__(self, broker, prefix):
        self.broker, self.prefix = broker, prefix
        self.paths = {1: ""}
        self.nodes = {"": 1}
        self.next_node = 2
        self.fd = None
        self.error = None

    def rpc(self, op, path="", **kwargs):
        return self.broker.call(
            {"op": op, "path": self.prefix + ("/" + path if path else ""), **kwargs}
        )

    def node(self, path):
        if path not in self.nodes:
            self.nodes[path] = self.next_node
            self.paths[self.next_node] = path
            self.next_node += 1
        return self.nodes[path]

    def attr(self, path):
        value = self.rpc("stat", path)
        return struct.pack(
            "<6Q10I",
            self.node(path),
            value["size"],
            (value["size"] + 511) // 512,
            value["atime"],
            value["mtime"],
            value["mtime"],
            0,
            0,
            0,
            value["mode"],
            value["nlink"],
            0,
            0,
            0,
            4096,
            0,
        )

    def entry(self, path):
        return struct.pack("<4Q2I", self.node(path), 1, 0, 0, 0, 0) + self.attr(path)

    def child(self, parent, name):
        validate_path(name)
        if "/" in name:
            raise PermissionError(errno.EACCES, "invalid entry name")
        return parent + "/" + name if parent else name

    def dispatch(self, opcode, nodeid, body):
        path = self.paths.get(nodeid, "")
        if nodeid not in self.paths and opcode != 26:
            raise OSError(errno.ESTALE, "unknown node")
        if opcode == 26:  # INIT, no writeback/parallel/dentry caching features.
            major, minor, readahead, _ = struct.unpack_from("<4I", body)
            if major != 7 or minor < 23:
                raise OSError(errno.EPROTO, "unsupported FUSE protocol")
            return struct.pack(
                "<4I2H2I2HI7I", 7, 23, readahead, 0, 16, 12, MAX_IO, 1, 0, 0, 0, *([0] * 7)
            )
        if opcode == 1:  # LOOKUP
            return self.entry(self.child(path, body.split(b"\0", 1)[0].decode("utf-8")))
        if opcode == 3:  # GETATTR
            return struct.pack("<QII", 0, 0, 0) + self.attr(path)
        if opcode == 4:  # SETATTR: size only; explicitly deny privilege/mode changes.
            valid = struct.unpack_from("<I", body)[0]
            if valid & ~(8 | 64):
                raise PermissionError(errno.EPERM, "unsupported attributes")
            if valid & 8:
                self.rpc("truncate", path, size=struct.unpack_from("<Q", body, 16)[0])
            return struct.pack("<QII", 0, 0, 0) + self.attr(path)
        if opcode in (14, 27):  # OPEN/OPENDIR
            mode = self.rpc("stat", path)["mode"]
            if stat.S_ISDIR(mode) != (opcode == 27):
                raise OSError(
                    errno.EISDIR if stat.S_ISDIR(mode) else errno.ENOTDIR, "wrong entry type"
                )
            return struct.pack("<QII", nodeid, 1 if opcode == 14 else 0, 0)
        if opcode == 15:  # READ
            _, offset, size = struct.unpack_from("<QQI", body)
            return base64.b64decode(self.rpc("read", path, offset=offset, size=min(size, MAX_IO)))
        if opcode == 16:  # WRITE
            _, offset, size = struct.unpack_from("<QQI", body)
            if size > MAX_IO or len(body[40:]) != size:
                raise OSError(errno.EINVAL, "invalid write")
            count = self.rpc(
                "write", path, offset=offset, data=base64.b64encode(body[40:]).decode("ascii")
            )
            return struct.pack("<II", count, 0)
        if opcode == 28:  # READDIR
            _, offset, size = struct.unpack_from("<QQI", body)
            names = [".", "..", *self.rpc("list", path)]
            output = bytearray()
            for index, name in enumerate(names[offset:], start=offset):
                relative = path if name in (".", "..") else self.child(path, name)
                try:
                    mode = self.rpc("stat", relative)["mode"]
                except OSError:
                    continue
                encoded = name.encode("utf-8")
                record = (
                    struct.pack(
                        "<QQII",
                        self.node(relative),
                        index + 1,
                        len(encoded),
                        4 if stat.S_ISDIR(mode) else 8,
                    )
                    + encoded
                )
                record += b"\0" * (-len(record) % 8)
                if len(output) + len(record) > size:
                    break
                output.extend(record)
            return bytes(output)
        if opcode == 35:  # CREATE
            name = body[16:].split(b"\0", 1)[0].decode("utf-8")
            child = self.child(path, name)
            self.rpc("create", child)
            return self.entry(child) + struct.pack("<QII", self.node(child), 1, 0)
        if opcode == 9:  # MKDIR
            child = self.child(path, body[8:].split(b"\0", 1)[0].decode("utf-8"))
            self.rpc("mkdir", child)
            return self.entry(child)
        if opcode in (10, 11):  # UNLINK/RMDIR
            child = self.child(path, body.split(b"\0", 1)[0].decode("utf-8"))
            self.rpc("unlink" if opcode == 10 else "rmdir", child)
            return b""
        if opcode in (12, 45):  # RENAME/RENAME2, no replacement or exchange.
            newdir = struct.unpack_from("<Q", body)[0]
            start = 8 if opcode == 12 else 16
            if opcode == 45 and struct.unpack_from("<I", body, 8)[0] not in (0, 1):
                raise PermissionError(errno.EPERM, "rename flags denied")
            old, new = body[start:].split(b"\0")[:2]
            source = self.child(path, old.decode("utf-8"))
            target = self.child(self.paths[newdir], new.decode("utf-8"))
            self.rpc("rename", source, destination=self.prefix + "/" + target)
            # Keep kernel node handles referring to the renamed subtree.
            for inode, existing in list(self.paths.items()):
                if existing == source or existing.startswith(source + "/"):
                    replacement = target + existing[len(source) :]
                    self.paths[inode] = replacement
                    self.nodes.pop(existing, None)
                    self.nodes[replacement] = inode
            return b""
        if opcode == 34:  # ACCESS: executable bits are absent from all files.
            mask = struct.unpack_from("<I", body)[0]
            mode = self.rpc("stat", path)["mode"]
            if mask & 1 and not stat.S_ISDIR(mode):
                raise PermissionError(errno.EACCES, "workspace executables denied")
            return b""
        if opcode in (18, 29, 25):  # RELEASE/RELEASEDIR/FLUSH
            return b""
        if opcode == 20:  # FSYNC
            self.rpc("sync", path)
            return b""
        if opcode == 17:  # STATFS: diagnostic does not promise workspace quota.
            return struct.pack("<5Q10I", 0, 0, 0, 0, 0, 4096, 255, 4096, *([0] * 7))
        if opcode in (6, 8, 13, 21, 22, 23, 24):
            raise PermissionError(errno.EPERM, "links/xattrs/mknod denied")
        raise OSError(errno.ENOSYS, "unsupported FUSE request")

    def serve(self):
        try:
            while True:
                request = os.read(self.fd, MAX_IO + 4096)
                if not request:
                    return
                length, opcode, unique, nodeid, _, _, _, _ = struct.unpack_from(
                    "<IIQQIIII", request
                )
                if length != len(request):
                    raise RuntimeError("malformed FUSE request")
                if opcode in (2, 42):  # FORGET has no reply.
                    continue
                if opcode == 38:  # DESTROY
                    return
                try:
                    payload, error = self.dispatch(opcode, nodeid, request[40:]), 0
                except OSError as exc:
                    payload, error = b"", -(exc.errno or errno.EIO)
                except (ValueError, KeyError, UnicodeError, struct.error):
                    payload, error = b"", -errno.EINVAL
                response = struct.pack("<IiQ", len(payload) + 16, error, unique) + payload
                os.write(self.fd, response)
        except OSError as exc:
            if exc.errno not in (errno.ENODEV, errno.EBADF):
                self.error = repr(exc)
        except Exception as exc:
            self.error = repr(exc)

    @contextlib.contextmanager
    def mounted(self):
        with tempfile.TemporaryDirectory(prefix="fixloop-s0-view-") as directory:
            self.fd = os.open("/dev/fuse", os.O_RDWR | os.O_CLOEXEC)
            mounted = False
            thread = None
            try:
                mount_fs(
                    "fixloop-s0",
                    directory,
                    "fuse",
                    f"fd={self.fd},rootmode=40000,user_id={os.getuid()},group_id={os.getgid()}",
                )
                mounted = True
                thread = threading.Thread(target=self.serve, daemon=True)
                thread.start()
                yield Path(directory)
            finally:
                if mounted:
                    unmount(directory)
                os.close(self.fd)
                if thread:
                    thread.join(timeout=3)
                    if thread.is_alive():
                        raise RuntimeError("FUSE worker did not terminate")
                if self.error:
                    raise RuntimeError("FUSE worker failed: " + self.error)
