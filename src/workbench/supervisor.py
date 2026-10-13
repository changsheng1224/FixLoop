"""Bounded worker cancellation and durable proof of OS/sandbox resource cleanup."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import src
from agent_runtime.cancellation import CancelledError
from agent_runtime.plan_runtime.processes import process_identity
from agent_runtime.state_root import native_io_path, project_id, workspace_id
from src.workbench.process_scope import CleanupUnconfirmedError, new_scope, reconcile_scope
from src.workbench.project import ProjectResult


class WorkerFailedError(RuntimeError):
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


def reconcile_sandbox(resource):
    if not resource.get("workspace"):
        return True
    try:
        from agent_runtime.linux_sandbox.task_context import checked_id

        source = resource["project_path"]
        pid = project_id(source)
        run_id = checked_id(resource["run_id"])
        if resource["project_id"] != pid or resource["workspace_id"] != workspace_id(
            resource["workspace"]
        ):
            return False
        path = (
            Path(resource["state_root"]) / "projects" / pid / "tasks" / run_id / "sandbox-task.json"
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False
    try:
        native_io_path(path).lstat()
    except FileNotFoundError:
        # The audited runtime cannot dispatch a sandbox operation before saving
        # its binding. Worker exit must have been proved before this check.
        return True
    except OSError:
        return False  # inaccessible is not absent; keep the cleanup gate closed
    try:
        from agent_runtime.linux_sandbox.task_context import SandboxTaskBinding
        from agent_runtime.linux_sandbox.workspace_backend import GuardedWindowsWorkspaceBackend

        binding = SandboxTaskBinding.load_record(path)
        if (
            binding.run_id != resource["run_id"]
            or binding.project_id != resource["project_id"]
            or binding.project_root != str(Path(source).resolve())
            or list(binding.project_directory_identity) != resource["root_identity"]
            or binding.mapping.host_root != resource["workspace"]
            or binding.mapping.workspace_id != resource["workspace_id"]
        ):
            return False
        backend = GuardedWindowsWorkspaceBackend(binding, retirement_only=True)
        current = backend.store.current()
        if current is None:
            return True
        receipt = backend.inspect_receipt(current["call_id"])
        return bool(
            receipt
            and receipt["state"] == "terminal"
            and (
                receipt.get("result", {}).get("cleanup") == "confirmed"
                or receipt.get("no_target_started") is True
            )
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def reconcile_execution(resource):
    if resource.get("state") == "stopped":
        return True
    if resource.get("state") == "allocated":
        return True  # no go message was sent; only the dead controller could materialize input
    return reconcile_scope(resource) and reconcile_sandbox(resource)


class ProcessWorkerRunner:
    def __init__(self, *, grace_s=1, cleanup_s=3):
        self.grace_s, self.cleanup_s = grace_s, cleanup_s

    def validate(self, executor, mode):
        executor.worker_spec()
        if hasattr(executor, "check_mode"):
            executor.check_mode(mode)

    def run(
        self,
        executor,
        *,
        mode,
        files,
        messages,
        token,
        on_chunk,
        publish,
        run_id,
        state_root,
        store,
        resource,
    ):
        spec = executor.worker_spec()
        state_root.mkdir(parents=True, exist_ok=True)
        request_path = state_root / "worker-request.json"
        request = {
            "spec": spec,
            "mode": mode,
            "run_id": run_id,
            "state_root": str(state_root),
            "workspace": str(files.root) if files else None,
            "project_root": resource.get("project_path"),
        }
        request["context"] = {
            "database": str(store.database),
            "conversation_id": resource["conversation_id"],
            "node_id": resource["node_id"],
            "snapshot_key": resource.get("input_key"),
        }
        with native_io_path(request_path).open("x", encoding="utf-8") as stream:
            json.dump(request, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        scope, process, reader = None, None, None
        frames = queue.Queue(maxsize=128)
        reading = threading.Event()
        descriptor = dict(resource)
        result, error = None, None
        try:
            scope = new_scope()
            descriptor.update(scope.descriptor(), state="starting")
            store.save_resource(run_id, descriptor)
            bootstrap = (
                "import sys;sys.path.insert(0,sys.argv[1]);"
                "from src.workbench.worker import main;main(sys.argv[2])"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    bootstrap,
                    str(Path(src.__file__).parent.parent),
                    str(request_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                cwd=state_root,
                close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            identity = process_identity(process.pid)
            if not identity or identity.get("generation") == "unknown":
                raise CleanupUnconfirmedError("worker generation unavailable")
            descriptor.update(worker=identity)
            store.save_resource(run_id, descriptor)
            scope.attach(process.pid)
            descriptor["state"] = "running"
            store.save_resource(run_id, descriptor)

            def receive():
                try:
                    while not reading.is_set():
                        line = process.stdout.readline(2_000_001)
                        if not line:
                            break
                        if len(line) > 2_000_000 or not line.endswith("\n"):
                            raise ValueError("worker frame limit exceeded")
                        frame = json.loads(line)
                        while not reading.is_set():
                            try:
                                frames.put(frame, timeout=0.05)
                                break
                            except queue.Full:
                                pass
                except Exception:
                    try:
                        frames.put_nowait(
                            {"kind": "error", "data": {"error": "worker_protocol_error"}}
                        )
                    except queue.Full:
                        pass

            reader = threading.Thread(target=receive, daemon=True, name="workbench-worker-output")
            reader.start()
            token.check()
            process.stdin.write("go\n")
            process.stdin.flush()
            deadline = time.monotonic() + executor.timeout_s
            stopping_at = None
            while result is None and error is None:
                now = time.monotonic()
                if now >= deadline and not token.is_cancelled:
                    token.cancel("timeout")
                if token.is_cancelled:
                    if stopping_at is None:
                        stopping_at = now
                        store.save_resource(
                            run_id, {**descriptor, "state": "cancelling", "reason": token.reason}
                        )
                        publish("run.cancelling", {"reason": token.reason})
                        try:
                            process.stdin.write(json.dumps({"cancel": token.reason}) + "\n")
                            process.stdin.flush()
                        except (OSError, BrokenPipeError):
                            pass
                    if now - stopping_at >= self.grace_s:
                        break
                try:
                    frame = frames.get(timeout=0.05)
                except queue.Empty:
                    if (
                        process.poll() is not None
                        and (reader is None or not reader.is_alive())
                        and frames.empty()
                    ):
                        error = "worker_exited_without_result"
                    continue
                kind, data = frame["kind"], frame["data"]
                if kind == "chunk" and not token.is_cancelled:
                    on_chunk(data["text"])
                elif kind == "progress" and not token.is_cancelled:
                    publish(data["kind"], data["payload"])
                elif kind == "result":
                    result = ProjectResult(**data)
                elif kind == "error":
                    error = data["error"]
                elif kind not in {"chunk", "progress"}:
                    error = "worker_protocol_error"
        finally:
            reading.set()
            try:
                if process is not None and descriptor["state"] != "running":
                    # The go gate is still closed: this child has never loaded
                    # requests or dispatched tools. Terminate it even if Job
                    # assignment failed, then wait for the direct process exit.
                    process.terminate()
                if scope is not None:
                    scope.terminate(self.cleanup_s)
                if process is not None:
                    process.wait(timeout=self.cleanup_s)
                if scope is not None and not reconcile_sandbox(descriptor):
                    raise CleanupUnconfirmedError("sandbox cleanup not confirmed")
                store.save_resource(run_id, {**descriptor, "state": "stopped"})
            except (OSError, subprocess.TimeoutExpired, CleanupUnconfirmedError) as exc:
                store.save_resource(run_id, {**descriptor, "state": "unknown"})
                raise CleanupUnconfirmedError("execution resource cleanup not confirmed") from exc
            finally:
                if scope is not None:
                    scope.close()
                if reader is not None:
                    reader.join(timeout=self.cleanup_s)
                if process is not None:
                    for stream in (process.stdin, process.stdout):
                        stream.close()
        token.check()
        if error is not None:
            raise WorkerFailedError(error)
        if result is None:
            raise CancelledError(token.reason or "worker stopped")
        return result
