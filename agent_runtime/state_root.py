"""Trusted external runtime state for sandboxed workspaces."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def workspace_id(root: str | Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


def trusted_state_root(root: str | Path, explicit: str | Path | None = None) -> Path | None:
    """Return the external state root only for an explicitly configured sandbox."""
    value = explicit or os.environ.get("FIXLOOP_STATE_ROOT", "")
    if not value:
        return None
    workspace = Path(root).resolve()
    state = Path(value).expanduser().resolve()
    if state == workspace or workspace in state.parents:
        raise ValueError("workspace_mapping_rejected: state_root inside workspace")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not state.is_dir():
        raise ValueError("workspace_mapping_rejected: invalid state_root")
    return state


def state_root_for(root: str | Path, explicit: str | Path | None = None) -> Path:
    return trusted_state_root(root, explicit) or Path(root).resolve()


def runtime_identity(
    *,
    backend: str = "",
    policy_digest: str = "",
    distribution_id: str = "",
    mapping_id: str = "",
    receipt_id: str = "",
    receipt_checksum: str = "",
) -> dict:
    return {
        "backend": backend,
        "policy_digest": policy_digest,
        "distribution_id": distribution_id,
        "mapping_id": mapping_id,
        "receipt_id": receipt_id,
        "receipt_checksum": receipt_checksum,
    }


__all__ = ["runtime_identity", "state_root_for", "trusted_state_root", "workspace_id"]
