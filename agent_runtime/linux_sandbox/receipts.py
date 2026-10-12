"""Trusted, atomic execution journal and cross-controller workspace lock."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from agent_runtime.state_root import native_io_path

if os.name == "posix":
    import fcntl


def _checksum(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read(path: Path) -> dict | None:
    path = native_io_path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(text)
        data = raw["payload"]
        if not isinstance(data, dict) or raw["sha256"] != _checksum(data):
            raise ValueError("receipt_invalid")
        return data
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt_invalid") from exc


def receipt_checksum(data: dict) -> str:
    return _checksum(data)


def _write(path: Path, data: dict) -> None:
    path = native_io_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({"payload": data, "sha256": _checksum(data)}, sort_keys=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            import ctypes

            # Replace + write-through: the directory entry must reach storage
            # before the broker is allowed to rename the original workspace file.
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            api.MoveFileExW.restype = ctypes.c_int

            def extended(value):
                absolute = os.path.abspath(value)
                if absolute.startswith("\\\\?\\"):
                    return absolute
                if absolute.startswith("\\\\"):
                    return "\\\\?\\UNC\\" + absolute[2:]
                return "\\\\?\\" + absolute

            # Python readers may briefly hold a handle without FILE_SHARE_DELETE.
            # Keep the same flushed temporary file and atomic write-through rename;
            # never delete the old marker or weaken its durability to bypass a reader.
            deadline = time.monotonic() + 1
            while not api.MoveFileExW(extended(temporary), extended(path), 0x1 | 0x8):
                error = ctypes.get_last_error()
                if error not in {5, 32, 33} or time.monotonic() >= deadline:
                    raise ctypes.WinError(error)
                time.sleep(0.005)
        else:
            os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _key(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()


def process_identity(pid: int) -> dict | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        return {
            "pid": pid,
            "start_ticks": int(fields[19]),  # field 22; fields begin at stat field 3
            "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid"),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, IndexError, ValueError):
        return None


class ReceiptStore:
    def __init__(self, state_root: Path, workspace: Path) -> None:
        self.root = state_root.resolve() / "linux_sandbox"
        self.key = _key(workspace)
        self.workspace = workspace.resolve()
        native_io_path(self.root).mkdir(mode=0o700, parents=True, exist_ok=True)

    @property
    def registry(self) -> Path:
        return self.root / f"{self.key}.json"

    def receipt_path(self, call_id: str) -> Path:
        return self.root / "receipts" / self.key / f"{call_id}.json"

    @contextmanager
    def lock(self):
        fd = os.open(native_io_path(self.root / f"{self.key}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"0")
                        os.fsync(fd)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise ValueError("workspace_busy") from exc
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def record_mutation(self, call_id: str, event: dict) -> None:
        """Persist each native commit intent before touching a directory entry."""
        current = self.current()
        if not current or current["call_id"] != call_id or current["state"] != "running":
            raise ValueError("receipt_invalid: mutation outside running call")
        events = [*current.get("mutation_events", []), event]
        if len(events) > 384:
            raise ValueError("receipt_invalid: too many mutation events")
        _write(self.registry, {**current, "mutation_events": events})

    def current(self) -> dict | None:
        data = _read(self.registry)
        if data and (
            data.get("workspace") != str(self.workspace)
            or data.get("state") not in {"planned", "running", "terminal"}
            or not isinstance(data.get("call_id"), str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", data["call_id"])
        ):
            raise ValueError("receipt_invalid: workspace identity")
        return data

    def transition(self, call_id: str, state: str, **fields) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", call_id):
            raise ValueError("policy_denied: call_id")
        previous = self.current()
        if state == "planned":
            if previous and previous["state"] != "terminal":
                raise ValueError("execution_uncertain: previous call has no terminal receipt")
            if self.receipt_path(call_id).exists():
                raise ValueError("policy_denied: duplicate call_id")
            data = {
                "workspace": str(self.workspace),
                "call_id": call_id,
                "state": state,
                "planned_at": time.time(),
                "sequence": (previous or {}).get("sequence", 0) + 1,
            }
        else:
            if not previous or previous["call_id"] != call_id:
                raise ValueError("receipt_invalid: call mismatch")
            allowed = {"running": {"planned"}, "terminal": {"running"}}
            if (
                state == "terminal"
                and previous["state"] == "terminal"
                and previous.get("result", {}).get("execution_status") == "uncertain"
                and previous.get("result", {}).get("cleanup") != "confirmed"
                and fields.get("recovery", {}).get("cleanup") == "confirmed"
                and fields.get("interrupted_result") == previous["result"]
            ):
                self.inspect(call_id)
                allowed["terminal"].add("terminal")
            if state == "terminal" and fields.get("no_target_started"):
                allowed["terminal"].add("planned")
            if previous["state"] not in allowed.get(state, set()):
                raise ValueError("receipt_invalid: invalid transition")
            data = {**previous, "state": state}
        data.update(fields)
        if state == "running":
            data["started_at"] = time.time()
        elif state == "terminal":
            data["ended_at"] = time.time()
        _write(self.registry, data)
        if state == "terminal":
            _write(self.receipt_path(call_id), data)
        return data

    def inspect(self, call_id: str) -> dict | None:
        """Read the exact call's durable receipt, including historical calls."""
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", call_id):
            raise ValueError("receipt_invalid: call_id")
        current = self.current()
        if current and current["call_id"] == call_id:
            if current["state"] == "terminal":
                if _read(self.receipt_path(call_id)) != current:
                    raise ValueError("receipt_invalid: terminal pair")
            return current
        receipt = _read(self.receipt_path(call_id))
        if receipt is not None and (
            receipt.get("call_id") != call_id
            or receipt.get("workspace") != str(self.workspace)
            or receipt.get("state") != "terminal"
        ):
            raise ValueError("receipt_invalid: historical receipt")
        return receipt

    def attach_target_identity(self, call_id: str, identity: dict | None) -> None:
        current = self.current()
        if not current or current["call_id"] != call_id or current["state"] != "running":
            raise ValueError("receipt_invalid: target identity transition")
        _write(self.registry, {**current, "target_identity": identity})

    def owned_versions(self, policy_digest: str) -> dict[str, str | None]:
        """Reconstruct rollback ownership from durable, confirmed native commits.

        The caller holds lock(). Current source versions must still match these
        values before restore; ownership never authorizes overwriting user edits.
        """
        from .workspace_contract import relative_path

        self.reconcile(policy_digest)
        directory = self.receipt_path("unused").parent
        records = []
        if directory.exists():
            for path in directory.iterdir():
                if len(records) >= 8192 or not path.is_file() or path.is_symlink():
                    raise ValueError("receipt_invalid: ownership journal budget or alias")
                receipt = self.inspect(path.stem)
                if not receipt or receipt.get("policy_digest") != policy_digest:
                    raise ValueError("resume_policy_mismatch")
                records.append(receipt)
        owned = {}
        for record in sorted(records, key=lambda item: (item.get("sequence", 0), item["ended_at"])):
            result = record.get("result", {})
            if (
                result.get("cleanup") != "confirmed"
                or result.get("execution_status") == "uncertain"
            ):
                raise ValueError("execution_uncertain: rollback ownership unavailable")
            versions = (
                result.get("tool_result", {}).get("metadata", {}).get("committed_versions", {})
            )
            if not isinstance(versions, dict):
                raise ValueError("receipt_invalid: committed versions")
            for name, version in versions.items():
                relative_path(name)
                if version is not None and not re.fullmatch(r"[a-f0-9]{64}", version):
                    raise ValueError("receipt_invalid: committed version")
                owned[name] = version
        return owned

    def reconcile(self, policy_digest: str) -> dict | None:
        data = self.current()
        if not data:
            return None
        if data.get("policy_digest") != policy_digest:
            raise ValueError("resume_policy_mismatch")
        if data["state"] != "terminal":
            identity = data.get("supervisor_identity")
            if data["state"] == "running" and identity:
                observed = process_identity(identity["pid"])
                if observed == identity:
                    raise ValueError("execution_uncertain: verified supervisor still active")
            raise ValueError("execution_uncertain: identity unverified; inspect workspace")
        receipt = _read(self.receipt_path(data["call_id"]))
        if receipt != data:
            raise ValueError("receipt_invalid")
        result = data.get("result", {})
        if result.get("execution_status") == "uncertain":
            raise ValueError("execution_uncertain: terminal mutation or execution unverified")
        if (
            result.get("cleanup") not in ("confirmed",)
            and result.get("execution_status") != "start_failed"
        ):
            raise ValueError("execution_uncertain: cleanup unverified")
        return data
