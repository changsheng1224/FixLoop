"""Task retirement preserves user source, worktrees and all recovery evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from pathlib import Path

from .receipts import _read, _write


def pin_environment(binding):
    """Persist an idle/waiting task reference before its backend is usable."""
    if not re.fullmatch(
        r"FixLoop-Sandbox-[a-z0-9-]{1,31}-[a-f0-9]{12}", binding.environment.distribution
    ):
        return  # In-process test transports do not represent managed distributions.
    from .environment import WslEnvironmentTransport, preparation_lock

    install = WslEnvironmentTransport().registration_path(binding.environment.distribution)
    if install is None:
        raise ValueError("environment_distribution_missing")
    directory = install.parent
    with preparation_lock(directory / "prepare.lock"):
        import json

        record = json.loads((directory / "installation.json").read_text("utf-8"))
        checksum = record.pop("checksum")
        from .environment import canonical, sha256

        if (
            checksum != sha256(canonical(record))
            or record["distribution"] != binding.environment.distribution
            or record["runtime_digest"] != binding.environment.runtime_digest
            or Path(record["install_path"]).resolve() != install.resolve()
            or record["stage"] != "ready"
        ):
            raise ValueError("environment_identity_invalid")
        key = hashlib.sha256(str(binding.state_root).encode()).hexdigest()
        _write(
            directory / "task-pins" / (key + ".json"),
            {
                "identity": binding.identity,
                "lifecycle": str(lifecycle_path(binding)),
            },
        )


def lifecycle_path(binding):
    return Path(binding.state_root) / "sandbox-lifecycle.json"


def assert_task_live(binding):
    assert_identity_live(binding.state_root, binding.identity)


def assert_identity_live(state_root, identity):
    record = _read(Path(state_root) / "sandbox-lifecycle.json")
    if record:
        if record.get("identity") != identity:
            raise ValueError("sandbox_task_identity_mismatch")
        if record.get("state") != "active":
            raise ValueError("sandbox_task_retired")


def retire_task(backend):
    """Explicit terminal retirement, retryable after a crash during native GC."""
    binding = backend.task_binding
    binding.validate()
    with backend.store.lock():
        backend._recover_locked()
        backend.store.reconcile(binding.policy_digest)
        record = _read(lifecycle_path(binding))
        if record and record.get("identity") != binding.identity:
            raise ValueError("sandbox_task_identity_mismatch")
        if record and record.get("state") == "retired":
            return record
        # Fence permission issuance and owner acquisition while publishing the
        # durable marker. Native GC happens afterwards, with admission closed.
        from agent_runtime.run_coordination.store import RunCoordinationStore

        store = RunCoordinationStore(binding.mapping.host_root, state_root=binding.state_root)
        with backend.permissions._locked() as authority:
            pending = authority.get("pending") or {}
            if authority.get("tickets") or pending.get("status") in {"pending", "approved"}:
                raise ValueError("sandbox_retirement_blocked: permission pending")
            with store.retirement_guard(binding.run_id):
                record = {"identity": binding.identity, "state": "retiring"}
                _write(lifecycle_path(binding), record)
        reply = backend._maintenance(
            {
                "maintenance": "retire_task",
                "task": {
                    "workspace_id": binding.mapping.workspace_id,
                    "task_id": binding.task_id,
                    "run_id": binding.run_id,
                },
                "environment": asdict(binding.environment),
            }
        )
        if reply.get("cleanup") != "confirmed":
            raise ValueError("sandbox_retirement_blocked: native cleanup unconfirmed")
        record = {**record, "state": "retired", "native": reply}
        _write(lifecycle_path(binding), record)
        return record
