"""Windows transport for fixed workspace tools over a protected FUSE view."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from agent_runtime.tool_result import ToolResult

from .models import SandboxRequest, SandboxResult
from .receipts import ReceiptStore
from .task_context import SandboxTaskBinding
from .workspace_broker import GuardedWorkspaceBroker
from .workspace_contract import WORKSPACE_WRITE_TOOLS, tool_result_to_wire
from .workspace_worker import WorkspaceReply
from .wsl_launcher import WslLauncherConfig


class GuardedWindowsWorkspaceBackend:
    """Task-bound production backend; no raw DrvFS/copy/host-tool fallback."""

    workspace_tools_only = False
    guarded_commands = True
    requires_authorization = True

    def __init__(self, task_binding: SandboxTaskBinding, *, retirement_only=False):
        from .lifecycle import assert_task_live, pin_environment
        from .permissions import PermissionService

        task_binding.validate()
        if not retirement_only:
            assert_task_live(task_binding)
            pin_environment(task_binding)
        self.task_binding = task_binding
        self.permissions = PermissionService.for_binding(task_binding)
        self.store = ReceiptStore(
            Path(task_binding.state_root), Path(task_binding.mapping.host_root)
        )
        self._active = {}
        self._guard = threading.Lock()
        self._closed = retirement_only

    def _argv(self):
        # Reuse the audited distro/user validation, but this entrypoint receives
        # no filesystem path or executable from the model.
        config = WslLauncherConfig(
            self.task_binding.environment.distribution,
            "/usr/bin/python3.14",
            "/opt/fixloop/runtime/agent_runtime/linux_sandbox/workspace_controller.py",
            "/opt/fixloop/runtime/payload.json",
            user="fixloop",
        )
        argv = config.argv()
        argv[0] = str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32/wsl.exe")
        # Only the fixed resource supervisor is privileged. It drops to uid/gid
        # 1000 before executing the existing controller or creating any view.
        argv[argv.index("--user") + 1] = "root"
        return argv[:-4] + [
            config.python,
            "-I",
            "-B",
            "/opt/fixloop/runtime/agent_runtime/linux_sandbox/resource_supervisor.py",
        ]

    def _validate(self, request):
        with self._guard:
            if self._closed:
                raise ValueError("sandbox_closed")
        from .lifecycle import assert_task_live

        assert_task_live(self.task_binding)
        if os.name != "nt":
            raise ValueError("workspace_mapping_rejected: native Windows profile required")
        self.task_binding.validate()
        request = SandboxRequest.from_wire(request.to_wire())
        if not 0 < request.timeout_s <= 120:
            raise ValueError("policy_denied: workspace tool profile")
        if request.operation != "workspace":
            from .command_profile import command_spec

            command_spec(request)
        if (
            request.workspace_id != self.task_binding.mapping.workspace_id
            or request.task_id != self.task_binding.task_id
            or request.run_id != self.task_binding.run_id
        ):
            raise ValueError("sandbox_task_identity_mismatch")
        self._assert_owner(request)

    def _assert_owner(self, request):
        if not (request.owner_token or request.generation or request.coordination_revision):
            return
        if not request.owner_token or request.generation <= 0:
            raise ValueError("stale_generation: incomplete owner envelope")
        from agent_runtime.run_coordination.store import RunCoordinationStore

        RunCoordinationStore(
            self.task_binding.mapping.host_root, state_root=self.task_binding.state_root
        ).assert_request_owner(
            request.run_id, request.owner_token, request.generation, request.coordination_revision
        )

    def execute(self, request):
        started = time.monotonic()
        planned = False
        broker = None
        try:
            self._validate(request)
            with self.store.lock():
                self._validate(request)
                self._recover_locked()
                self.store.reconcile(self.task_binding.policy_digest)
                if (self.store.current() or {}).get("sequence", 0) >= 8192:
                    raise ValueError("resource_limit_exceeded: receipt budget")
                broker = self._permission_broker(request)
                inputs = broker.permission_versions(request)
                permission = self.permissions.consume(request, input_versions=inputs)
                self.store.transition(
                    request.call_id,
                    "planned",
                    policy_digest=self.task_binding.policy_digest,
                    **{
                        key: getattr(request, key)
                        for key in (
                            "workspace_id",
                            "task_id",
                            "run_id",
                            "owner_token",
                            "generation",
                            "coordination_revision",
                        )
                    },
                    mapping_id=self.task_binding.mapping.mapping_id,
                    environment_id=self.task_binding.environment.identity_id,
                    tool_name=request.tool_name,
                    permission=permission,
                )
                planned = True
                owned_broker, broker = broker, None
                result = self._execute_locked(request, started, permission, owned_broker)
                self.permissions.outcome(permission, result.execution_status, result.receipt_id)
                return result
        except (OSError, ValueError) as exc:
            if planned:
                try:
                    self.permissions.outcome(permission, "uncertain", request.call_id)
                except (OSError, ValueError, KeyError):
                    pass
            return SandboxResult(
                "uncertain" if planned else "rejected",
                error_code="execution_uncertain" if planned else str(exc).split(":", 1)[0],
                receipt_id=request.call_id,
                policy_digest=self.task_binding.policy_digest,
                cleanup="unverified" if planned else "confirmed",
                mutation_status="unknown" if planned else "not_started",
                permission=permission if planned else {},
            )
        finally:
            if broker is not None:
                broker.close()

    def _permission_broker(self, request):
        from .workspace_inventory import SKIP_DIRS

        return GuardedWorkspaceBroker(
            Path(self.task_binding.mapping.host_root),
            hidden_dirs=SKIP_DIRS
            if request.operation != "workspace"
            or request.tool_name
            in {
                "workspace_inventory",
                "workspace_snapshot",
                "workspace_environment",
                "workspace_text",
            }
            else (),
        )

    def permission_inputs(self, request):
        self._validate(request)
        broker = self._permission_broker(request)
        try:
            return broker.permission_versions(request)
        finally:
            broker.close()

    def _execute_locked(self, request, started, permission, broker):
        proc = None
        cancelled = threading.Event()
        result = SandboxResult(
            "uncertain",
            error_code="execution_uncertain",
            receipt_id=request.call_id,
            policy_digest=self.task_binding.policy_digest,
            owner_token=request.owner_token,
            generation=request.generation,
            coordination_revision=request.coordination_revision,
            permission=permission,
        )
        terminal, errors = [], []
        reader = None
        writer = None
        terminal_valid = False
        transport_lock = threading.Lock()
        transport_closed = threading.Event()
        outbound = queue.Queue(maxsize=1)
        deferred_cleanup = threading.Event()
        broker_cleanup_lock = threading.Lock()

        def close_broker():
            nonlocal broker
            with broker_cleanup_lock:
                if broker is not None:
                    broker.close()
                    broker = None

        def disconnect():
            transport_closed.set()
            if proc is not None:
                # A controller that stops reading must not pin the cancelling
                # thread inside write/flush or a lock held by that writer.
                if not transport_lock.acquire(timeout=0.1):
                    return
                try:
                    if not proc.stdin.closed:
                        proc.stdin.close()
                finally:
                    transport_lock.release()

        def send():
            try:
                while not transport_closed.is_set():
                    try:
                        frame = outbound.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    with transport_lock:
                        if transport_closed.is_set():
                            return
                        proc.stdin.write(frame)
                        proc.stdin.flush()
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                # A blocked write may finish only after teardown kills the
                # controller. The writer then owns closing its pipe.
                if transport_closed.is_set():
                    with transport_lock:
                        try:
                            proc.stdin.close()
                        except (OSError, ValueError) as exc:
                            errors.append(exc)

        try:
            # Keep native handles open until the RPC reader has stopped, even
            # when transport validation or cancellation fails.
            if broker is not None:
                self.permissions.assert_current(permission)
                self.store.transition(request.call_id, "running")
                with self._guard:
                    if self._closed:
                        raise ValueError("sandbox_closed")
                    proc = subprocess.Popen(
                        self._argv(),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                    )
                    self._active[request.call_id] = cancelled

                def serve():
                    try:
                        while True:
                            frame = proc.stdout.readline(2_000_001)
                            if not frame or len(frame) > 2_000_000 or not frame.endswith(b"\n"):
                                raise ValueError("workspace_result_invalid: controller frame")
                            raw = json.loads(frame)
                            if isinstance(raw, dict) and set(raw) == {"terminal"}:
                                terminal.append(raw["terminal"])
                                return
                            if not isinstance(raw, dict) or set(raw) != {"rpc"}:
                                raise ValueError("workspace_result_invalid: broker frame")
                            try:
                                answer = {"result": broker.call(raw["rpc"])}
                            except OSError as exc:
                                answer = {"errno": exc.errno or 5}
                            except (ValueError, KeyError, TypeError):
                                answer = {"errno": 22}
                            outbound.put_nowait(
                                (json.dumps(answer, ensure_ascii=True) + "\n").encode()
                            )
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        KeyError,
                        RecursionError,
                        queue.Full,
                    ) as exc:
                        errors.append(exc)
                    finally:
                        if deferred_cleanup.is_set():
                            try:
                                close_broker()
                            except (OSError, ValueError) as exc:
                                errors.append(exc)
                            proc.stdout.close()

                envelope = {
                    "request": request.to_wire(),
                    "environment": asdict(self.task_binding.environment),
                }
                outbound.put_nowait((json.dumps(envelope, ensure_ascii=True) + "\n").encode())
                reader = threading.Thread(target=serve, daemon=True)
                writer = threading.Thread(target=send, daemon=True)
                reader.start()
                writer.start()
                deadline = time.monotonic() + request.timeout_s + 30
                while (
                    reader.is_alive()
                    and not errors
                    and not cancelled.is_set()
                    and time.monotonic() < deadline
                ):
                    reader.join(timeout=0.05)
                if reader.is_alive():
                    cancelled.set()
                    disconnect()
                    reader.join(timeout=8)
                if reader.is_alive() or errors or not terminal:
                    raise ValueError("execution_uncertain: no trusted terminal frame")
                proc.wait(timeout=3)
                observed = terminal[0]
                if (
                    proc.returncode != 0
                    or not isinstance(observed, dict)
                    or set(observed)
                    != {"call_id", "cleanup", "cancelled", "exit_code", "reply", "runtime_digest"}
                    or observed["call_id"] != request.call_id
                    or observed["cleanup"] != "confirmed"
                    or type(observed["cancelled"]) is not bool
                    or type(observed["exit_code"]) is not int
                    or observed["runtime_digest"] != self.task_binding.environment.runtime_digest
                ):
                    raise ValueError("workspace_result_invalid: terminal identity")
                terminal_valid = True
                broker.release_reads()
                if observed["cancelled"] or cancelled.is_set():
                    result = SandboxResult(
                        **{
                            **result.to_wire(),
                            "execution_status": "cancelled",
                            "error_code": "cancelled",
                            "cleanup": "confirmed",
                            "actual_backend": "linux_sandbox",
                            "mutation_status": "not_started",
                        }
                    )
                else:
                    if observed["exit_code"] != 0 or observed["reply"] is None:
                        raise ValueError("workspace_result_invalid: worker did not reply")
                    reply = WorkspaceReply.from_wire(observed["reply"], request)
                    outcome = None
                    if request.tool_name in WORKSPACE_WRITE_TOOLS and reply.mutations:

                        def assert_owner():
                            if cancelled.is_set():
                                raise ValueError("cancelled: commit cancelled")
                            self._assert_owner(request)
                            self.permissions.assert_current(permission)

                        outcome = broker.commit(
                            request,
                            reply,
                            record=lambda event: self.store.record_mutation(request.call_id, event),
                            assert_owner=assert_owner,
                        )
                        if outcome.error_code:
                            reply.result = ToolResult(
                                "Error: conditional workspace commit failed",
                                status="error",
                                error_code=outcome.error_code,
                                retryable=False,
                                changed_files=outcome.changed_files,
                                metadata={
                                    "recovery_files": outcome.recovery_files,
                                    "committed_versions": {
                                        item.path: hashlib.sha256(item.content.encode()).hexdigest()
                                        if item.content is not None
                                        else None
                                        for item in reply.mutations
                                        if item.path in outcome.changed_files
                                    },
                                },
                            )
                            if outcome.error_code == "execution_uncertain":
                                reply.result.status = "uncertain"
                        else:
                            reply.result = ToolResult(
                                "workspace edit committed",
                                changed_files=outcome.changed_files,
                                metadata={
                                    "committed_versions": {
                                        item.path: hashlib.sha256(item.content.encode()).hexdigest()
                                        if item.content is not None
                                        else None
                                        for item in reply.mutations
                                        if item.path in outcome.changed_files
                                    }
                                },
                            )
                    result = SandboxResult(
                        **{
                            **result.to_wire(),
                            "execution_status": "cancelled"
                            if reply.result.status == "cancelled"
                            else "uncertain"
                            if outcome and outcome.error_code == "execution_uncertain"
                            else "completed",
                            "error_code": outcome.error_code if outcome else "",
                            "exit_code": 0,
                            "cleanup": "confirmed",
                            "actual_backend": "linux_sandbox",
                            "mutation_status": "partial"
                            if outcome and outcome.error_code and outcome.changed_files
                            else "committed"
                            if outcome and outcome.changed_files
                            else "not_started",
                            "tool_result": tool_result_to_wire(reply.result),
                            **(
                                {
                                    "exit_code": reply.result.metadata.get("command_exit_code"),
                                    "stdout_excerpt": reply.result.metadata.get(
                                        "command_stdout", ""
                                    ),
                                    "stderr_excerpt": reply.result.metadata.get(
                                        "command_stderr", ""
                                    ),
                                    "output_truncated": reply.result.output_truncated,
                                    "error_code": reply.result.error_code,
                                }
                                if request.operation != "workspace"
                                else {}
                            ),
                        }
                    )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            subprocess.SubprocessError,
        ) as exc:
            if proc is None:
                result = SandboxResult(
                    **{
                        **result.to_wire(),
                        "execution_status": "start_failed",
                        "cleanup": "confirmed",
                        "mutation_status": "not_started",
                        "error_code": str(exc).split(":", 1)[0],
                    }
                )
            elif terminal_valid:
                try:
                    current = self.store.current()
                except (OSError, ValueError):
                    current = None
                if current is not None and not current.get("mutation_events"):
                    result = SandboxResult(
                        **{
                            **result.to_wire(),
                            "execution_status": "completed",
                            "exit_code": 0,
                            "cleanup": "confirmed",
                            "actual_backend": "linux_sandbox",
                            "error_code": str(exc).split(":", 1)[0],
                            "mutation_status": "not_started",
                            "tool_result": tool_result_to_wire(
                                ToolResult.error(
                                    "Error: " + str(exc),
                                    code=str(exc).split(":", 1)[0],
                                    retryable=False,
                                )
                            ),
                        }
                    )
        finally:
            with self._guard:
                self._active.pop(request.call_id, None)
            cleanup_failed = False

            def cleanup(action):
                nonlocal cleanup_failed
                try:
                    action()
                except (OSError, ValueError, subprocess.SubprocessError):
                    cleanup_failed = True

            if proc is not None:
                cleanup(disconnect)
                if proc.poll() is None:
                    cleanup(proc.kill)
                    cleanup(lambda: proc.wait(timeout=5))
                if reader is not None:
                    deferred_cleanup.set()
                    reader.join(timeout=3)
                if writer is not None:
                    writer.join(timeout=3)
                    if writer.is_alive():
                        cleanup_failed = True
                    else:
                        cleanup(disconnect)
                if reader is None or not reader.is_alive():
                    cleanup(proc.stdout.close)
                else:
                    cleanup_failed = True
            if broker is not None and (reader is None or not reader.is_alive()):
                cleanup(close_broker)
            if cleanup_failed:
                result = SandboxResult(
                    **{
                        **result.to_wire(),
                        "execution_status": "uncertain",
                        "error_code": "execution_uncertain",
                        "cleanup": "unverified",
                    }
                )
        result = SandboxResult(
            **{**result.to_wire(), "duration_ms": round((time.monotonic() - started) * 1000)}
        )
        try:
            self.store.transition(
                request.call_id, "terminal", result=result.to_wire(), no_target_started=proc is None
            )
        except (OSError, ValueError):
            # A missing terminal receipt must never turn an already applied edit
            # into a rejected/not-started operation.
            result = SandboxResult(
                **{
                    **result.to_wire(),
                    "execution_status": "uncertain",
                    "error_code": "execution_uncertain",
                }
            )
        return result

    def cancel(self, call_id):
        with self._guard:
            active = self._active.get(call_id)
            if active is None:
                return False
            active.set()
            return True

    def inspect_receipt(self, call_id):
        receipt = self.store.inspect(call_id)
        if receipt and (
            receipt["state"] != "terminal"
            or (
                receipt.get("result", {}).get("execution_status") == "uncertain"
                and receipt.get("result", {}).get("cleanup") != "confirmed"
            )
        ):
            try:
                with self.store.lock():
                    self._recover_locked()
            except (OSError, ValueError) as exc:
                # Receipt inspection is an audit read.  If native recovery is
                # unavailable, return the durable uncertain receipt and let the
                # next execution attempt remain blocked by ``_recover_locked``.
                # Identity and policy mismatches still fail closed.
                code = str(exc).split(":", 1)[0]
                if code not in {"workspace_busy", "execution_uncertain"}:
                    raise
            receipt = self.store.inspect(call_id)
        return receipt

    def _maintenance(self, payload):
        envelope = {**payload, "environment": asdict(self.task_binding.environment)}
        try:
            proc = subprocess.run(
                self._argv(),
                input=(json.dumps(envelope) + "\n").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("execution_uncertain: maintenance failed") from exc
        if len(proc.stdout) > 2_000_000:
            raise ValueError("execution_uncertain: maintenance frame")
        return json.loads(proc.stdout)

    def _recover_locked(self):
        from .resource_supervisor import execution_key

        current = self.store.current()
        if not current:
            return
        if current["state"] == "terminal":
            self.store.inspect(current["call_id"])  # Validate the durable terminal pair first.
            prior = current.get("result", {})
            if prior.get("execution_status") != "uncertain" or prior.get("cleanup") == "confirmed":
                return
        if (
            current.get("policy_digest") != self.task_binding.policy_digest
            or current.get("environment_id") != self.task_binding.environment.identity_id
            or current.get("mapping_id") != self.task_binding.mapping.mapping_id
        ):
            raise ValueError("resume_policy_mismatch")
        from types import SimpleNamespace

        key = execution_key(
            SimpleNamespace(
                **{name: current[name] for name in ("workspace_id", "task_id", "run_id", "call_id")}
            ),
            self.task_binding.environment.runtime_digest,
        )
        evidence = self._maintenance({"maintenance": "reconcile", "key": key})
        if evidence.get("key") != key or evidence.get("cleanup") != "confirmed":
            raise ValueError("execution_uncertain: native recovery unconfirmed")
        mutations = bool(current.get("mutation_events"))
        result = SandboxResult(
            "uncertain" if mutations else "cancelled",
            error_code="execution_uncertain" if mutations else "recovered_interruption",
            receipt_id=current["call_id"],
            policy_digest=self.task_binding.policy_digest,
            cleanup="confirmed",
            mutation_status="unknown" if mutations else "not_started",
            owner_token=current.get("owner_token", ""),
            generation=current.get("generation", 0),
            coordination_revision=current.get("coordination_revision", 0),
            permission=current.get("permission", {}),
        )
        self.store.transition(
            current["call_id"],
            "terminal",
            result=result.to_wire(),
            no_target_started=current["state"] == "planned",
            recovery=evidence,
            **({"interrupted_result": current["result"]} if current["state"] == "terminal" else {}),
        )
        if result.permission:
            self.permissions.outcome(result.permission, result.execution_status, result.receipt_id)

    def recover(self):
        """Called under the replacement coordinator's reconciliation fence."""
        with self.store.lock():
            self._recover_locked()
            self.store.reconcile(self.task_binding.policy_digest)
            return self.store.current()

    def owned_versions(self):
        with self.store.lock():
            self._recover_locked()
            self.store.reconcile(self.task_binding.policy_digest)
            return self.store.owned_versions(self.task_binding.policy_digest)

    def close(self, timeout_s=12):
        """Idempotently close admission and cancel all calls owned by this object."""
        with self._guard:
            self._closed = True
            for event in self._active.values():
                event.set()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._guard:
                idle = not self._active
            if idle:
                try:
                    return self.recover()
                except ValueError as exc:
                    if str(exc).split(":", 1)[0] != "workspace_busy":
                        raise
            time.sleep(0.02)
        raise ValueError("execution_uncertain: backend shutdown incomplete")

    def retire(self):
        from .lifecycle import retire_task

        return retire_task(self)

    @classmethod
    def resume_retirement(cls, binding):
        """Resume interrupted GC without reopening tool admission or environment pins."""
        return cls(binding, retirement_only=True).retire()
