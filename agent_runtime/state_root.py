"""Trusted external runtime state for sandboxed workspaces."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def workspace_id(root: str | Path) -> str:
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()[:16]


def managed_state_base(root: str | Path, explicit: str | Path | None = None) -> Path:
    """Product-owned storage, never an implicit in-checkout .agent directory."""
    value = explicit or os.environ.get("FIXLOOP_STATE_ROOT")
    if not value:
        if os.name == "nt":
            parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            parent = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        value = parent / "FixLoop" / "state" / workspace_id(root)
    return trusted_state_root(root, value)


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


def agent_state_root(agent) -> Path:
    """Use one configured location for all of an Agent's durable memory/cache."""
    return state_root_for(
        getattr(agent, "_cwd", ".") or ".",
        getattr(getattr(agent, "tool_context", None), "state_root", "") or "",
    )


def native_io_path(path: str | Path) -> Path:
    """Use Windows extended paths for deep external-state files at the I/O boundary."""
    value = Path(path)
    if os.name != "nt":
        return value
    raw = str(value.resolve())
    if raw.startswith("\\\\?\\"):
        return Path(raw)
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


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


__all__ = [
    "agent_state_root",
    "managed_state_base",
    "native_io_path",
    "runtime_identity",
    "state_root_for",
    "trusted_state_root",
    "workspace_id",
]
