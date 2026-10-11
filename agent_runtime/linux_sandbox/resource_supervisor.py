"""Fixed root control plane; project code runs as fixloop in a bounded cgroup.

The root process only measures product files, manages its own cgroups and relays
bounded frames. It never mounts, reads or changes the user workspace.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    package = types.ModuleType("agent_runtime")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules["agent_runtime"] = package

from agent_runtime.linux_sandbox.receipts import _read, _write, process_identity

CGROUP = Path("/sys/fs/cgroup/fixloop-sandbox")
STATE = Path("/var/lib/fixloop/executions")
SCRATCH = Path("/var/lib/fixloop/scratch")
CONTROLLER = "/opt/fixloop/runtime/agent_runtime/linux_sandbox/workspace_controller.py"
MEMORY_BYTES = 512 * 1024 * 1024
DEPENDENCY_BYTES = 256 * 1024 * 1024
DEPENDENCY_ENTRIES = 8192


def execution_key(request, runtime_digest):
    material = [
        request.workspace_id,
        request.task_id,
        request.run_id,
        request.call_id,
        runtime_digest,
    ]
    return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()


def prepare_cgroups():
    """Fail closed if the kernel cannot enforce the complete resource profile."""
    base = CGROUP.parent
    if not {"cpu", "memory", "pids"} <= set((base / "cgroup.controllers").read_text().split()):
        raise ValueError("resource_limits_unavailable")
    (base / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    CGROUP.mkdir(exist_ok=True)
    for name, value in {
        "memory.max": str(2 * 1024**3),
        "memory.swap.max": "0",
        "pids.max": "256",
        "cpu.max": "200000 100000",
        "cgroup.subtree_control": "+cpu +memory +pids",
    }.items():
        (CGROUP / name).write_text(value)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.lstat().st_uid != 0 or STATE.lstat().st_mode & 0o077:
        raise ValueError("resource_state_invalid")


@contextlib.contextmanager
def execution_lock(key):
    import fcntl

    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("resource_identity_invalid")
    # Retain anti-replay evidence, but never allow an unbounded native journal.
    # A full environment can be explicitly retired after its tasks are retired.
    admission = os.open(STATE / "admission.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(admission, fcntl.LOCK_EX)
        lock = STATE / (key + ".lock")
        if not lock.exists() and sum(1 for _ in STATE.iterdir()) >= 65537:
            raise ValueError("resource_limit_exceeded: execution journal budget")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    finally:
        os.close(admission)
    try:
        if os.fstat(fd).st_uid != 0 or os.fstat(fd).st_nlink != 1:
            raise ValueError("resource_state_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def kill_group(group):
    if not group.exists():
        return
    (group / "cgroup.kill").write_text("1")
    deadline = time.monotonic() + 5
    while "populated 1" in (group / "cgroup.events").read_text():
        if time.monotonic() >= deadline:
            raise ValueError("execution_uncertain: cgroup not empty")
        time.sleep(0.02)
    group.rmdir()


def cleanup_scratch(key):
    directory = SCRATCH / key
    if directory.is_symlink():
        raise ValueError("execution_uncertain: scratch alias")
    if directory.exists():
        shutil.rmtree(directory)


def create_scratch(key):
    SCRATCH.mkdir(mode=0o711, exist_ok=True)
    info = SCRATCH.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("resource_state_invalid")
    directory = SCRATCH / key
    directory.mkdir(mode=0o700)
    os.chown(directory, 1000, 1000)
    return directory


def emit(frame):
    """A stalled or disconnected host must not keep the root supervisor alive."""
    data = memoryview((json.dumps(frame) + "\n").encode())
    descriptor = sys.stdout.fileno()
    os.set_blocking(descriptor, False)
    deadline = time.monotonic() + 5
    while data:
        if time.monotonic() >= deadline:
            raise ValueError("execution_uncertain: host output blocked")
        if select.select([], [descriptor], [], 0.05)[1]:
            try:
                data = data[os.write(descriptor, data) :]
            except BlockingIOError:
                pass


def dependency_usage(root):
    size, count = 0, 0
    if not root.exists():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            try:
                info = (Path(directory) / name).lstat()
            except FileNotFoundError:
                continue  # pip may rename its own staging files during the scan.
            size += info.st_size
            count += 1
            if stat.S_ISLNK(info.st_mode) or size > DEPENDENCY_BYTES or count > DEPENDENCY_ENTRIES:
                raise ValueError("resource_limit_exceeded: dependency quota")


def child_limits(group):
    """Runs before exec, before threads, and before dropping all root identities."""
    import resource

    (group / "cgroup.procs").write_text(str(os.getpid()))
    for kind, limit in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_FSIZE, 64 * 1024**2),
        (resource.RLIMIT_CPU, 120),
    ):
        resource.setrlimit(kind, (limit, limit))
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)


def reconcile(key):
    """Stop only the exact execution, and fence a delayed start with a tombstone."""
    group = CGROUP / key
    deadline = time.monotonic() + 8
    while True:
        try:
            with execution_lock(key):
                record = _read(STATE / (key + ".json"))
                kill_group(group)
                cleanup_scratch(key)
                # Tombstones also cover a crash between host Popen and native startup.
                _write(
                    STATE / (key + ".json"),
                    {"state": "terminal", "key": key, "cleanup": "confirmed", "recovered": True},
                )
                return {"key": key, "cleanup": "confirmed", "previous": record}
        except BlockingIOError:
            # Only the root-owned cgroup with this sealed execution key is touched.
            if group.exists():
                (group / "cgroup.kill").write_text("1")
            if time.monotonic() >= deadline:
                raise ValueError("execution_uncertain: supervisor still active")
            time.sleep(0.05)


def retire_dependencies(task, measured):
    """Acquire the same native flock before collecting only task-owned data."""
    import fcntl
    from types import SimpleNamespace

    from agent_runtime.linux_sandbox.task_dependencies import TASK_BASE, task_environment, task_key

    if set(task) != {"workspace_id", "task_id", "run_id"} or any(
        not isinstance(value, str) or not value or len(value) > 256 for value in task.values()
    ):
        raise ValueError("resource_identity_invalid")
    request = SimpleNamespace(**task)
    key = task_key(request, measured)
    root = TASK_BASE / key
    # Let the existing validator create/check parents under the dedicated uid.
    if not root.exists():

        def create():
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)
            with task_environment(request, measured, retiring=True):
                pass

        pid = os.fork()
        if pid == 0:
            try:
                create()
                os._exit(0)
            except BaseException:
                os._exit(1)
        if os.waitpid(pid, 0)[1] != 0:
            raise ValueError("sandbox_task_environment_invalid")
    for directory in (TASK_BASE.parent, TASK_BASE, root):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 or info.st_mode & 0o022:
            raise ValueError("sandbox_task_environment_invalid")
    fd = os.open(root / "lease", os.O_RDWR | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != 1000 or os.fstat(fd).st_nlink != 1:
            raise ValueError("sandbox_task_environment_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write(root / "retired.json", {"key": key, "state": "retiring"})
        deps = root / "deps"
        if deps.is_symlink():
            raise ValueError("sandbox_task_environment_invalid")
        if deps.exists():
            shutil.rmtree(deps)  # Linux fd-based rmtree does not follow symlinks.
        _write(root / "retired.json", {"key": key, "state": "retired"})
        return {"key": key, "cleanup": "confirmed"}
    finally:
        os.close(fd)


def supervise(envelope, request, measured):
    from agent_runtime.linux_sandbox.task_dependencies import TASK_BASE, task_key
    from agent_runtime.linux_sandbox.workspace_worker import WorkspaceReply
    from agent_runtime.tool_result import ToolResult

    key = execution_key(request, measured["runtime_digest"])
    with execution_lock(key):
        path, group = STATE / (key + ".json"), CGROUP / key
        if path.exists() or group.exists():
            raise ValueError("resource_execution_replayed")
        _write(
            path,
            {
                "state": "running",
                "key": key,
                "started_at": time.time(),
                "supervisor_identity": process_identity(os.getpid()),
            },
        )
        proc, terminal, error = None, None, ""
        stopped = threading.Event()
        writer = None
        try:
            scratch = create_scratch(key)
            group.mkdir()
            for name, value in {
                "memory.max": str(MEMORY_BYTES),
                "memory.swap.max": "0",
                "memory.oom.group": "1",
                "pids.max": "64",
                "cpu.max": "100000 100000",
            }.items():
                (group / name).write_text(value)
            proc = subprocess.Popen(
                ["/usr/bin/python3.14", "-I", "-B", CONTROLLER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": str(scratch)},
                close_fds=True,
                preexec_fn=lambda: child_limits(group),
            )

            def relay_input():
                try:
                    proc.stdin.write((json.dumps(envelope) + "\n").encode())
                    proc.stdin.flush()
                    while not stopped.is_set():
                        if not select.select([sys.stdin.fileno()], [], [], 0.05)[0]:
                            continue
                        chunk = os.read(sys.stdin.fileno(), 65536)
                        if not chunk:
                            break
                        proc.stdin.write(chunk)
                        proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                finally:
                    proc.stdin.close()

            writer = threading.Thread(target=relay_input, daemon=True)
            writer.start()
            deadline = time.monotonic() + request.timeout_s + 20
            pending = bytearray()
            os.set_blocking(proc.stdout.fileno(), False)
            while time.monotonic() < deadline:
                dependency_usage(TASK_BASE / task_key(request, measured) / "deps")
                if select.select([proc.stdout], [], [], 0.05)[0]:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, pending = pending.partition(b"\n")
                        if len(line) > 2_000_000:
                            raise ValueError("workspace_result_invalid")
                        frame = json.loads(line)
                        if set(frame) == {"terminal"}:
                            if terminal is not None:
                                raise ValueError("workspace_result_invalid")
                            terminal = frame["terminal"]
                        elif set(frame) == {"rpc"} and terminal is None:
                            emit(frame)
                        else:
                            raise ValueError("workspace_result_invalid")
                    if len(pending) > 2_000_000:
                        raise ValueError("workspace_result_invalid")
                if proc.poll() is not None and terminal is not None:
                    break
            if terminal is None:
                error = (
                    "resource_limit_exceeded" if proc.poll() is not None else "deadline_exceeded"
                )
            events = dict(
                line.split() for line in (group / "memory.events").read_text().splitlines()
            )
            if int(events.get("oom_kill", 0)):
                error = "resource_limit_exceeded"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = str(exc).split(":", 1)[0]
        finally:
            stopped.set()
            kill_group(group)
            if proc is not None:
                proc.wait(timeout=5)
                if writer is not None:
                    writer.join(timeout=2)
                    if writer.is_alive():
                        raise ValueError("execution_uncertain: supervisor input remains open")
                proc.stdout.close()
            cleanup_scratch(key)
        _write(
            path,
            {
                "state": "terminal",
                "key": key,
                "cleanup": "confirmed",
                "error_code": error,
                "ended_at": time.time(),
            },
        )
        if error:
            terminal = {
                "call_id": request.call_id,
                "cleanup": "confirmed",
                "cancelled": False,
                "exit_code": 0,
                "runtime_digest": measured["runtime_digest"],
                "reply": WorkspaceReply(
                    ToolResult.error(error, code=error, retryable=False)
                ).to_wire(),
            }
        emit({"terminal": terminal})


def main():
    if os.getuid() != 0 or sys.argv[1:]:
        raise ValueError("resource_supervisor_profile_invalid")
    from agent_runtime.linux_sandbox.environment_worker import measure
    from agent_runtime.linux_sandbox.models import SandboxRequest

    # No caller-controlled filesystem paths or executable names enter the root plane.
    frame = bytearray()
    deadline = time.monotonic() + 10
    while b"\n" not in frame and len(frame) <= 1_100_000:
        if time.monotonic() >= deadline:
            raise ValueError("policy_denied: supervisor input timeout")
        if select.select([sys.stdin.fileno()], [], [], 0.05)[0]:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            frame.extend(chunk)
    if not frame.endswith(b"\n") or frame.count(b"\n") != 1 or len(frame) > 1_100_000:
        raise ValueError("policy_denied: supervisor frame")
    envelope = json.loads(frame)
    measured = measure()
    if envelope.get("environment", {}).get("runtime_digest") != measured["runtime_digest"]:
        raise ValueError("sandbox_environment_identity_mismatch")
    prepare_cgroups()
    if (
        set(envelope) == {"maintenance", "key", "environment"}
        and envelope["maintenance"] == "reconcile"
    ):
        emit(reconcile(envelope["key"]))
    elif (
        set(envelope) == {"maintenance", "task", "environment"}
        and envelope["maintenance"] == "retire_task"
    ):
        emit(retire_dependencies(envelope["task"], measured))
    elif set(envelope) == {"request", "environment"}:
        request = SandboxRequest.from_wire(envelope["request"])
        if not 0 < request.timeout_s <= 120:
            raise ValueError("policy_denied: resource deadline")
        supervise(envelope, request, measured)
    else:
        raise ValueError("policy_denied: supervisor envelope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
