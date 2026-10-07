"""One-call Linux supervisor. The target never receives its control pipe."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

# Invoked with -I from a trusted native helper path, never from the task checkout.
if __package__ in (None, ""):
    trusted_root = Path(__file__).resolve().parents[2]
    package = types.ModuleType("agent_runtime")
    package.__path__ = [str(trusted_root / "agent_runtime")]
    sys.modules["agent_runtime"] = package
    sys.path.insert(0, str(trusted_root))
    from agent_runtime.linux_sandbox.models import SandboxRequest, SandboxResult
    from agent_runtime.linux_sandbox.policy import SandboxPolicy
    from agent_runtime.linux_sandbox.receipts import ReceiptStore, process_identity
else:
    from .models import SandboxRequest, SandboxResult
    from .policy import SandboxPolicy
    from .receipts import ReceiptStore, process_identity


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _collect(
    proc: subprocess.Popen, timeout_s: float, limit: int
) -> tuple[str, bytes, bytes, bool]:
    selector = selectors.DefaultSelector()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    os.set_blocking(sys.stdin.fileno(), False)
    selector.register(sys.stdin, selectors.EVENT_READ, "control")
    deadline = time.monotonic() + timeout_s
    status = "completed"
    overflow = False
    try:
        while proc.poll() is None or len(selector.get_map()) > 1:
            if time.monotonic() >= deadline:
                status = "timeout"
                break
            for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                if key.data == "control":
                    if not os.read(key.fd, 4096):
                        status = "cancelled"
                        break
                    status = "cancelled"  # No second message is part of this protocol.
                    break
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = max(0, limit - total)
                streams[key.data].extend(chunk[:remaining])
                total += len(chunk)
                if total > limit:
                    status = "output_limit_exceeded"
                    overflow = True
                    break
            if status != "completed":
                break
        return status, bytes(streams["stdout"]), bytes(streams["stderr"]), overflow
    finally:
        selector.close()


def _cleanup(proc: subprocess.Popen, terminate: bool) -> tuple[str, int]:
    started = time.monotonic()
    if terminate and proc.poll() is None:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        proc.stdout.close()
        proc.stderr.close()
        return "failed", int((time.monotonic() - started) * 1000)
    # A descendant retaining either output pipe means cleanup is not proven.
    selector = selectors.DefaultSelector()
    try:
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        end = time.monotonic() + 3
        while selector.get_map() and time.monotonic() < end:
            for key, _ in selector.select(0.05):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
        return (
            "confirmed" if not selector.get_map() else "failed",
            int((time.monotonic() - started) * 1000),
        )
    finally:
        selector.close()
        proc.stdout.close()
        proc.stderr.close()


def supervise(policy: SandboxPolicy, request: SandboxRequest, digest: str) -> SandboxResult:
    store = ReceiptStore(policy.state_root, policy.workspace)
    started = time.monotonic()
    store.transition(request.call_id, "running", supervisor_identity=process_identity(os.getpid()))
    try:
        from agent_runtime.linux_sandbox.backend import _validate_owner_envelope

        _validate_owner_envelope(policy, request)
        proc = subprocess.Popen(
            policy.argv(request),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            cwd="/",
        )
    except (OSError, ValueError) as exc:
        result = SandboxResult(
            "rejected" if isinstance(exc, ValueError) else "start_failed",
            error_code=str(exc).split(":", 1)[0]
            if isinstance(exc, ValueError)
            else "tool_start_failed",
            cleanup="confirmed",
            mutation_status="not_started",
            receipt_id=request.call_id,
            policy_digest=digest,
            owner_token=request.owner_token,
            generation=request.generation,
            coordination_revision=request.coordination_revision,
        )
        store.transition(
            request.call_id, "terminal", result=result.to_wire(), no_target_started=True
        )
        return result
    store.attach_target_identity(request.call_id, process_identity(proc.pid))
    startup_ms = int((time.monotonic() - started) * 1000)
    status, stdout, stderr, overflow = _collect(proc, request.timeout_s, request.output_limit_bytes)
    cleanup, cleanup_ms = _cleanup(proc, status != "completed")
    if cleanup != "confirmed":
        status = "uncertain"
    elif status == "completed" and proc.returncode != 0:
        status = "completed"  # Nonzero is an observed command result, not an infra failure.
    codes = {
        "timeout": "tool_timeout",
        "cancelled": "tool_cancelled",
        "output_limit_exceeded": "output_limit_exceeded",
        "uncertain": "process_cleanup_failed",
    }
    result = SandboxResult(
        status,
        exit_code=proc.returncode,
        error_code=codes.get(status, ""),
        stdout_excerpt=stdout[:8192].decode("utf-8", "replace"),
        stderr_excerpt=stderr[:8192].decode("utf-8", "replace"),
        output_truncated=overflow or len(stdout) > 8192 or len(stderr) > 8192,
        cleanup=cleanup,
        receipt_id=request.call_id,
        policy_digest=digest,
        duration_ms=int((time.monotonic() - started) * 1000),
        startup_ms=startup_ms,
        cleanup_ms=cleanup_ms,
        actual_backend="linux_sandbox",
        mutation_status="pending",
        owner_token=request.owner_token,
        generation=request.generation,
        coordination_revision=request.coordination_revision,
    )
    store.transition(request.call_id, "terminal", result=result.to_wire())
    return result


def main() -> None:
    try:
        envelope = json.loads(sys.stdin.buffer.readline(65537))
        raw = envelope["policy"]
        policy = SandboxPolicy(
            **{
                key: Path(value)
                if key
                in {"workspace", "state_root", "toolchain", "helper", "bwrap", "python", "lib_dir"}
                else value
                for key, value in raw.items()
            }
        )
        request = SandboxRequest.from_wire(envelope["request"])
        policy.validate()
        policy.validate_request(request)
        digest = policy.digest()
        if (
            digest != envelope["policy_digest"]
            or policy.helper.resolve() != Path(__file__).resolve()
        ):
            raise ValueError("resume_policy_mismatch")
        result = supervise(policy, request, digest)
        print(json.dumps(result.to_wire()), flush=True)
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"execution_status": "uncertain", "error_code": str(exc)[:120]}), flush=True
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
