"""Controller-facing P1 backend; not registered as an Agent tool."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict

from .models import SandboxRequest, SandboxResult
from .policy import SandboxPolicy
from .receipts import ReceiptStore


def _validate_owner_envelope(policy: SandboxPolicy, request: SandboxRequest) -> None:
    """Fail closed for coordination-bound requests before a supervisor starts."""
    if not (request.owner_token or request.generation or request.coordination_revision):
        return
    if not request.owner_token or request.generation <= 0:
        raise ValueError("stale_generation: incomplete owner envelope")
    from agent_runtime.run_coordination.store import CoordinationError, RunCoordinationStore

    try:
        RunCoordinationStore(
            str(policy.workspace), state_root=str(policy.state_root)
        ).assert_request_owner(
            request.run_id,
            request.owner_token,
            request.generation,
            request.coordination_revision,
        )
    except CoordinationError as exc:
        raise ValueError(exc.code) from exc


class LinuxSandboxBackend:
    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self.store = ReceiptStore(policy.state_root, policy.workspace)
        self._active: dict[str, subprocess.Popen] = {}
        self._guard = threading.Lock()

    def preflight(self) -> str:
        self.policy.validate()
        try:
            digest = self.policy.digest()
        except OSError as exc:
            raise ValueError("toolchain_unavailable: policy identity") from exc
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                port = listener.getsockname()[1]
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
                probe = SandboxRequest(
                    "preflight",
                    "preflight",
                    "preflight",
                    "preflight",
                    "command",
                    (
                        "/toolchain/bin/python",
                        "-I",
                        "-c",
                        "import os,json,pathlib,socket,sys; s=socket.socket(); s.settimeout(1); "
                        "print(json.dumps({'pid':os.getpid(), "
                        "'tmp':os.statvfs('/tmp').f_blocks*os.statvfs('/tmp').f_frsize, "
                        "'state':pathlib.Path(sys.argv[2]).exists(), "
                        "'mnt':pathlib.Path('/mnt/c').exists(), "
                        "'run':pathlib.Path('/run/WSL').exists(), "
                        "'interop':pathlib.Path('/proc/sys/fs/binfmt_misc/WSLInterop').exists(), "
                        "'net':s.connect_ex(('127.0.0.1',int(sys.argv[1]))), "
                        "'env':sorted(os.environ)}))",
                        str(port),
                        str(self.policy.state_root.resolve()),
                    ),
                    timeout_s=10,
                )
                proc = subprocess.run(
                    self.policy.argv(probe),
                    capture_output=True,
                    timeout=12,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    cwd="/",
                    check=False,
                )
                observed = json.loads(proc.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired, KeyError) as exc:
            raise ValueError("namespace_unavailable") from exc
        expected_env = {
            "HOME",
            "PATH",
            "PWD",
            "TMPDIR",
            "LANG",
            "LC_CTYPE",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        }
        if (
            proc.returncode
            or observed["pid"] != 2
            or observed["tmp"] != self.policy.temp_limit_bytes
        ):
            raise ValueError("namespace_unavailable")
        if (
            observed["state"]
            or observed["mnt"]
            or observed["run"]
            or observed["interop"]
            or set(observed["env"]) - expected_env
        ):
            raise ValueError("interop_isolation_failed")
        if observed["net"] == 0:
            raise ValueError("network_isolation_failed")
        return digest

    def reconcile(self) -> dict | None:
        digest = self.preflight()
        with self.store.lock():
            return self.store.reconcile(digest)

    def inspect_receipt(self, call_id: str) -> dict | None:
        return self.store.inspect(call_id)

    def execute(self, request: SandboxRequest) -> SandboxResult:
        try:
            digest = self.preflight()
            self.policy.validate_request(request)
            _validate_owner_envelope(self.policy, request)
            with self.store.lock():
                self.store.reconcile(digest)
                self.store.transition(
                    request.call_id,
                    "planned",
                    policy_digest=digest,
                    task_id=request.task_id,
                    run_id=request.run_id,
                    workspace_id=request.workspace_id,
                    owner_token=request.owner_token,
                    generation=request.generation,
                    coordination_revision=request.coordination_revision,
                    mapping_id=str(self.policy.workspace.resolve()),
                    distribution_id="wsl2",
                )
                return self._dispatch(request, digest)
        except (OSError, ValueError) as exc:
            code = str(exc).split(":", 1)[0]
            return SandboxResult(
                "rejected",
                error_code=code,
                receipt_id=request.call_id if isinstance(request, SandboxRequest) else "",
            )

    def _dispatch(self, request: SandboxRequest, digest: str) -> SandboxResult:
        policy_data = asdict(self.policy)
        envelope = {
            "request": request.to_wire(),
            "policy_digest": digest,
            "policy": {
                key: str(value) if hasattr(value, "__fspath__") else value
                for key, value in policy_data.items()
            },
        }
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", str(self.policy.helper)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                cwd="/",
            )
        except OSError:
            result = SandboxResult(
                "start_failed",
                error_code="tool_start_failed",
                cleanup="confirmed",
                mutation_status="not_started",
                receipt_id=request.call_id,
                policy_digest=digest,
                owner_token=request.owner_token,
                generation=request.generation,
                coordination_revision=request.coordination_revision,
            )
            self.store.transition(
                request.call_id, "terminal", result=result.to_wire(), no_target_started=True
            )
            return result
        try:
            with self._guard:
                self._active[request.call_id] = proc
            proc.stdin.write((json.dumps(envelope) + "\n").encode())
            proc.stdin.flush()
            raw = self._read_response(proc, request.timeout_s + 9)
            if raw is None and not proc.stdin.closed:
                proc.stdin.close()
                raw = self._read_response(proc, 5)
            if raw is not None:
                proc.wait(timeout=3)
                payload = json.loads(raw)
                if proc.returncode == 0 and payload.get("receipt_id") == request.call_id:
                    receipt = self.store.reconcile(digest)
                    if (
                        receipt
                        and receipt.get("result") == payload
                        and self._receipt_matches_request(receipt, request)
                    ):
                        result = SandboxResult(**payload)
                        return result
            return SandboxResult(
                "uncertain",
                error_code="execution_uncertain",
                receipt_id=request.call_id,
                policy_digest=digest,
                owner_token=request.owner_token,
                generation=request.generation,
                coordination_revision=request.coordination_revision,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return SandboxResult(
                "uncertain",
                error_code="execution_uncertain",
                receipt_id=request.call_id,
                policy_digest=digest,
                owner_token=request.owner_token,
                generation=request.generation,
                coordination_revision=request.coordination_revision,
            )
        finally:
            with self._guard:
                self._active.pop(request.call_id, None)
            if "proc" in locals():
                if not proc.stdin.closed:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                if proc.poll() is None:
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                proc.stdout.close()

    @staticmethod
    def _receipt_matches_request(receipt: dict, request: SandboxRequest) -> bool:
        return all(
            receipt.get(key) == request.__dict__[key]
            for key in (
                "task_id",
                "run_id",
                "workspace_id",
                "owner_token",
                "generation",
                "coordination_revision",
            )
        )

    @staticmethod
    def _read_response(proc: subprocess.Popen, timeout_s: float) -> bytes | None:
        deadline = time.monotonic() + timeout_s
        data = bytearray()
        while time.monotonic() < deadline and len(data) <= 65536:
            ready, _, _ = select.select(
                [proc.stdout], [], [], min(0.1, max(0, deadline - time.monotonic()))
            )
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), min(4096, 65537 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in data:
                return bytes(data.split(b"\n", 1)[0])
        return None

    def cancel(self, call_id: str) -> bool:
        with self._guard:
            proc = self._active.get(call_id)
            if proc is None or proc.stdin.closed:
                return False
            proc.stdin.close()
            return True
