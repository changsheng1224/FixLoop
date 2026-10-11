"""Fixed Linux controller for a read-only guarded workspace tool worker.

The native broker channel stays outside bwrap. This entrypoint lives in the
verified product runtime, never in the user repository or on a Windows mount.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import queue
import select
import subprocess
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

# -I excludes cwd and environment paths; only the fixed product closure is added.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    package = types.ModuleType("agent_runtime")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules["agent_runtime"] = package

from agent_runtime.linux_sandbox.guarded_fs import FuseView, RemoteBroker, private_binfmt
from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.workspace_contract import MAX_REQUEST_BYTES, MAX_RESULT_BYTES

RUNTIME = Path("/opt/fixloop/runtime")
# This fixed data/AST worker does not use the wider P1 command profile (dbm,
# readline, sqlite). Its libraries must match the S2 minimal prepared image.
WORKSPACE_LIBRARIES = (
    "libbz2.so.1.0",
    "libc.so.6",
    "libcrypto.so.3",
    "libexpat.so.1",
    "libffi.so.8",
    "liblzma.so.5",
    "libm.so.6",
    "libssl.so.3",
    "libz.so.1",
    "libzstd.so.1",
)


def enter_namespaces() -> None:
    """Enter private namespaces only after measuring real distro ownership.

    Root-owned files become overflow-uid files in the task user's mapped
    namespace, so integrity measurement must precede this transition. Do it
    before starting any threads, without changing the environment verifier.
    """
    uid, gid = os.getuid(), os.getgid()
    if uid != 1000 or gid == 0:
        raise ValueError("policy_denied: dedicated task user required")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(0x10000000):  # CLONE_NEWUSER
        raise OSError(ctypes.get_errno(), "private user namespace failed")
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"0 {uid} 1\n")
    Path("/proc/self/gid_map").write_text(f"0 {gid} 1\n")
    if libc.unshare(0x00020000):  # CLONE_NEWNS
        raise OSError(ctypes.get_errno(), "private mount namespace failed")
    # Prevent mount propagation to the distro, even if its root is shared.
    if libc.mount(None, b"/", None, 0x40000 | 0x4000, None):  # MS_PRIVATE | MS_REC
        raise OSError(ctypes.get_errno(), "private mount propagation failed")


class ControllerBroker(RemoteBroker):
    """One binary channel reader supplies RPC responses and observes host EOF."""

    def __init__(self):
        super().__init__()
        self.disconnected = threading.Event()
        self.stopped = threading.Event()
        self.responses = queue.Queue(maxsize=1)
        self.receiver = threading.Thread(target=self._receive, daemon=True)
        self.receiver.start()

    def _receive(self):
        try:
            pending = bytearray()
            descriptor = sys.stdin.fileno()
            while not self.stopped.is_set():
                if not select.select([descriptor], [], [], 0.05)[0]:
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, remaining = pending.partition(b"\n")
                    if len(line) + 1 > MAX_RESULT_BYTES:
                        raise ValueError("broker response exceeds limit")
                    self.responses.put_nowait(json.loads(line))
                    pending = bytearray(remaining)
                if len(pending) >= MAX_RESULT_BYTES:
                    break
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            self.disconnected.set()

    def close(self):
        self.stopped.set()
        self.receiver.join(timeout=1)
        if self.receiver.is_alive():
            raise ValueError("execution_uncertain: broker reader remains open")

    def call(self, request):
        with self.lock:
            print(json.dumps({"rpc": request}, ensure_ascii=True), flush=True)
            while not self.disconnected.is_set():
                try:
                    response = self.responses.get(timeout=0.05)
                except queue.Empty:
                    continue
                if not isinstance(response, dict):
                    break
                if set(response) == {"errno"} and type(response["errno"]) is int:
                    raise OSError(response["errno"], "broker denied operation")
                if set(response) == {"result"}:
                    return response["result"]
                break
            raise OSError(errno.EIO, "broker disconnected")


def worker_argv(view: Path, *, command=None, dependencies=None) -> list[str]:
    lib = "/usr/lib/x86_64-linux-gnu"
    argv = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--dir",
        "/usr",
        "--dir",
        "/usr/bin",
        "--dir",
        "/usr/lib",
        "--dir",
        lib,
        "--dir",
        "/lib64",
        "--ro-bind",
        "/usr/bin/python3.14",
        "/usr/bin/python3.14",
        "--ro-bind",
        "/usr/lib/python3.14",
        "/usr/lib/python3.14",
        "--ro-bind",
        lib + "/ld-linux-x86-64.so.2",
        "/lib64/ld-linux-x86-64.so.2",
    ]
    for name in WORKSPACE_LIBRARIES:
        argv += ["--ro-bind", lib + "/" + name, lib + "/" + name]
    argv += [
        "--ro-bind",
        str(RUNTIME / "agent_runtime"),
        "/executor/agent_runtime",
        "--ro-bind",
        str(view),
        "/workspace",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        "67108864",
        "--tmpfs",
        "/tmp",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--chdir",
        "/workspace",
        "--",
        "/usr/bin/python3.14",
        "-I",
        "-B",
        "-c",
        "import sys, types; sys.path.insert(0, '/executor'); "
        "p=types.ModuleType('agent_runtime'); p.__path__=['/executor/agent_runtime']; "
        "sys.modules['agent_runtime']=p; "
        "from agent_runtime.linux_sandbox.workspace_worker import main; raise SystemExit(main())",
    ]
    if command is not None:
        from agent_runtime.linux_sandbox.command_profile import bootstrap, command_spec

        mode, args = command_spec(command)
        split = argv.index("--")
        argv = argv[:split]
        # Project code receives no product package, controller channel or lease.
        start = argv.index(str(RUNTIME / "agent_runtime")) - 1
        del argv[start : start + 3]
        argv += [
            "--ro-bind",
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/ssl/certs/ca-certificates.crt",
            "--ro-bind",
            "/opt/fixloop/toolchain",
            "/toolchain",
            "--bind" if mode == "install" else "--ro-bind",
            str(dependencies),
            "/task/deps",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "1",
            "--",
            "/usr/bin/python3.14",
            "-I",
            "-S",
            "-B",
            "-c",
            bootstrap(mode, args, command.cwd_relative),
        ]
    return argv


def run_worker(argv, request, broker, *, command_output=False):
    """Bound stdin, output draining and teardown, including failed startup."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        close_fds=True,
    )
    output, errors, transport_errors = [], [], []

    def read_output(pipe, target, limit):
        try:
            target.append(pipe.read(limit + 1))
            while pipe.read(65536):
                pass
        except (OSError, ValueError) as exc:
            transport_errors.append(exc)

    def write_input():
        try:
            proc.stdin.write((json.dumps(request.to_wire(), ensure_ascii=True) + "\n").encode())
        except (OSError, ValueError) as exc:
            transport_errors.append(exc)
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError) as exc:
                transport_errors.append(exc)

    threads = [
        threading.Thread(
            target=read_output,
            args=(
                proc.stdout,
                output,
                request.output_limit_bytes if command_output else MAX_RESULT_BYTES,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=read_output,
            args=(proc.stderr, errors, request.output_limit_bytes if command_output else 4096),
            daemon=True,
        ),
    ]
    if command_output:
        proc.stdin.close()
    else:
        threads.append(threading.Thread(target=write_input, daemon=True))
    cancelled = False
    try:
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + request.timeout_s
        while proc.poll() is None:
            if time.monotonic() >= deadline or broker.disconnected.is_set():
                cancelled = True
                break
            if transport_errors:
                break
            time.sleep(0.02)
    finally:
        if proc.poll() is None:
            proc.kill()  # PID-namespace init teardown kills every worker process.
        proc.wait(timeout=5)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=3)
        if any(thread.is_alive() for thread in threads):
            raise ValueError("execution_uncertain: worker transport remains open")
        proc.stdout.close()
        proc.stderr.close()
    if transport_errors and not cancelled:
        raise ValueError("workspace_result_invalid: worker transport failed")
    reply = None
    if command_output:
        limit = request.output_limit_bytes
        reply = {
            "stdout": (output[0] if output else b"")[:limit].decode("utf-8", "replace"),
            "stderr": (errors[0] if errors else b"")[:limit].decode("utf-8", "replace"),
            "truncated": any(len(data[0]) > limit for data in (output, errors) if data),
            "exit_code": proc.returncode,
        }
    elif not cancelled and proc.returncode == 0 and output and len(output[0]) <= MAX_RESULT_BYTES:
        reply = json.loads(output[0])
    return proc.returncode, cancelled, reply


def environment_reply(mount, request, broker, dependencies, measured):
    from agent_runtime.linux_sandbox.task_dependencies import dependency_version, task_key
    from agent_runtime.linux_sandbox.workspace_worker import WorkspaceReply

    internal = replace(
        request,
        operation="workspace",
        argv=(),
        schema_version="2",
        tool_name="workspace_inventory",
        tool_args={},
        base_versions={},
    )
    code, cancelled, raw = run_worker(worker_argv(mount), internal, broker)
    if cancelled:
        error = "cancelled" if broker.disconnected.is_set() else "deadline_exceeded"
        raise ValueError(error + ": inventory interrupted")
    if code != 0 or raw is None:
        raise ValueError("snapshot_unavailable: no inventory reply")
    reply = WorkspaceReply.from_wire(raw, internal)
    if not reply.result.ok:
        raise ValueError(reply.result.error_code + ": inventory unavailable")
    return {
        "source_version": reply.result.metadata["content_version"],
        "dependency_version": dependency_version(dependencies),
        "task_environment": task_key(request, measured),
        "runtime_digest": measured["runtime_digest"],
    }


def run_command(mount, request, broker, dependencies, measured):
    from agent_runtime.linux_sandbox.workspace_worker import WorkspaceReply
    from agent_runtime.tool_result import ToolResult

    deadline = time.monotonic() + request.timeout_s

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0 or broker.disconnected.is_set():
            error = "cancelled" if broker.disconnected.is_set() else "deadline_exceeded"
            raise ValueError(error + ": command environment interrupted")
        return replace(request, timeout_s=budget)

    before = environment_reply(mount, remaining(), broker, dependencies, measured)
    code, cancelled, output = run_worker(
        worker_argv(mount, command=request, dependencies=dependencies),
        remaining(),
        broker,
        command_output=True,
    )
    # Evidence is assembled outside the namespace that executed project code.
    after = None
    if not cancelled:
        try:
            after = environment_reply(mount, remaining(), broker, dependencies, measured)
        except ValueError:
            # A completed command must never acquire fresh evidence from a failed scan.
            after = None
    stable = before == after
    metadata = {
        "command_exit_code": code,
        "command_stdout": output["stdout"],
        "command_stderr": output["stderr"],
        "verification_binding": before,
        "environment_after": after,
        "verification_current": stable,
        "workspace_read_only": True,
    }
    result = ToolResult(
        "command completed" if not cancelled else "command interrupted",
        metadata=metadata,
        status="success" if code == 0 else "error",
        error_code="" if code == 0 else "command_failed",
        retryable=False,
        output_truncated=output["truncated"],
    )
    if cancelled:
        result.status = "cancelled"
        result.error_code = "deadline_exceeded" if not broker.disconnected.is_set() else "cancelled"
    elif request.operation == "pytest" and not stable:
        result.status = "error"
        result.error_code = "verification_stale"
    return 0, False, WorkspaceReply(result).to_wire()


def main() -> int:
    frame = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 4097)
    if not frame.endswith(b"\n") or len(frame) > MAX_REQUEST_BYTES + 4096:
        raise ValueError("policy_denied: incomplete controller request")
    envelope = json.loads(frame)
    if set(envelope) != {"request", "environment"}:
        raise ValueError("policy_denied: controller envelope")
    request = SandboxRequest.from_wire(envelope["request"])
    if request.timeout_s > 120:
        raise ValueError("policy_denied: workspace profile")
    if request.operation != "workspace":
        from agent_runtime.linux_sandbox.command_profile import command_spec

        command_spec(request)
    from agent_runtime.linux_sandbox.environment_worker import measure

    measured = measure()
    expected = envelope["environment"]
    if (
        expected["runtime_digest"] != measured["runtime_digest"]
        or expected["distribution_version"] != measured["distribution_version"]
        or expected["runtime_version"] != measured["runtime_version"]
    ):
        raise ValueError("sandbox_environment_identity_mismatch")
    from agent_runtime.linux_sandbox.task_dependencies import task_environment

    # Every tool owns the same native task lease, including source-only reads.
    with task_environment(request, measured) as dependencies:
        return serve_request(request, measured, dependencies)


def serve_request(request, measured, dependencies):
    enter_namespaces()
    broker = ControllerBroker()
    view = FuseView(broker, "")
    # This process now owns a fresh mapped user/mount namespace. PE binfmt
    # is privately disabled before creating the further isolated bwrap namespace.
    try:
        with private_binfmt(), view.mounted() as mount:
            from agent_runtime.linux_sandbox.workspace_worker import WorkspaceReply
            from agent_runtime.tool_result import ToolResult

            try:
                if request.operation != "workspace":
                    exit_code, cancelled, reply = run_command(
                        mount, request, broker, dependencies, measured
                    )
                elif request.tool_name == "workspace_environment":
                    data = environment_reply(mount, request, broker, dependencies, measured)
                    exit_code, cancelled, reply = (
                        0,
                        False,
                        WorkspaceReply(ToolResult(data=data)).to_wire(),
                    )
                else:
                    exit_code, cancelled, reply = run_worker(worker_argv(mount), request, broker)
            except ValueError as exc:
                error = str(exc).split(":", 1)[0]
                if error == "execution_uncertain":
                    raise  # A worker still alive cannot claim confirmed cleanup.
                result = ToolResult.error(str(exc), code=error, retryable=False)
                if error in {"cancelled", "deadline_exceeded"}:
                    result.status = "cancelled"
                exit_code, cancelled, reply = 0, False, WorkspaceReply(result).to_wire()
    finally:
        broker.close()
    # The FUSE thread and private mounts have now been cleaned up; no worker
    # ever received stdout/stdin handles for the broker or the native journal.
    print(
        json.dumps(
            {
                "terminal": {
                    "call_id": request.call_id,
                    "cleanup": "confirmed",
                    "cancelled": cancelled,
                    "exit_code": exit_code,
                    "reply": reply,
                    "runtime_digest": measured["runtime_digest"],
                }
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
