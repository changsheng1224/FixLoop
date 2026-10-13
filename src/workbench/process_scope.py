"""OS resource scopes. A process exit alone never proves descendant cleanup."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from agent_runtime.plan_runtime.processes import confirmed_exited
from agent_runtime.process_scope import CleanupUnconfirmedError, WindowsJob


class LinuxCgroup:
    """Requires a delegated cgroup v2 subtree; never fall back to process groups."""

    backend = "linux_cgroup"

    def __init__(self, root=None, *, existing=False):
        base = os.environ.get("FIXLOOP_WORKER_CGROUP_ROOT", "")
        if not existing and not base:
            raise CleanupUnconfirmedError("delegated worker cgroup unavailable")
        self.root = Path(root) if existing else Path(base) / ("fixloop-" + uuid.uuid4().hex)
        resolved = self.root.resolve()
        if not resolved.is_relative_to(Path("/sys/fs/cgroup")):
            raise CleanupUnconfirmedError("worker cgroup must be below /sys/fs/cgroup")
        if not existing:
            self.root.mkdir()
            try:
                (self.root / "pids.max").write_text("128")
                if not (self.root / "cgroup.kill").exists():
                    raise CleanupUnconfirmedError("cgroup.kill unavailable")
            except BaseException:
                self.close()
                raise

    def attach(self, pid):
        (self.root / "cgroup.procs").write_text(str(pid))

    def empty(self):
        return "populated 1" not in (self.root / "cgroup.events").read_text()

    def terminate(self, timeout=3):
        (self.root / "cgroup.kill").write_text("1")
        deadline = time.monotonic() + timeout
        while not self.empty():
            if time.monotonic() >= deadline:
                raise CleanupUnconfirmedError("worker cgroup still populated")
            time.sleep(0.02)

    def descriptor(self):
        return {"backend": self.backend, "scope": str(self.root)}

    def close(self):
        if self.root.exists() and self.empty():
            self.root.rmdir()


def new_scope():
    return WindowsJob() if os.name == "nt" else LinuxCgroup()


def reconcile_scope(resource):
    """Terminate a durable scope and prove it empty before reusing artifacts."""
    backend, identity = resource.get("backend"), resource.get("worker", {})
    if resource.get("state") == "starting" and not identity:
        # Durable starting precedes process registration and the go gate. A
        # child in this window has no request and EOF exits its trusted bootstrap.
        return True
    try:
        if backend == "windows_job" and os.name == "nt":
            try:
                scope = WindowsJob(resource["scope"], existing=True)
            except OSError as exc:
                if exc.winerror == 2 and confirmed_exited(identity):
                    return True
                return False
        elif backend == "linux_cgroup" and os.name == "posix":
            try:
                Path(resource["scope"]).lstat()
            except FileNotFoundError:
                return confirmed_exited(identity)
            scope = LinuxCgroup(resource["scope"], existing=True)
        else:
            return False
        try:
            scope.terminate()
            return scope.empty() and (not identity or confirmed_exited(identity))
        finally:
            scope.close()
    except (OSError, KeyError, CleanupUnconfirmedError):
        return False
