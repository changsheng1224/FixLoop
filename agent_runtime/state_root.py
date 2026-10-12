"""Trusted external runtime state for sandboxed workspaces."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def workspace_id(root: str | Path) -> str:
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()[:16]


def project_id(root: str | Path) -> str:
    """Identity of a registered source path, independent of Git/content versions.

    Managed worktrees and snapshot copies must carry their source root explicitly.
    Neither matching remotes nor matching commits merge independent clones.
    """
    return "project-" + workspace_id(root)


def managed_storage_root(
    root: str | Path, explicit: str | Path | None = None, *, create: bool = True,
) -> Path:
    """Resolve shared product storage. Callers must not pass a project/run directory."""
    value = explicit or os.environ.get("FIXLOOP_STATE_ROOT")
    if not value:
        if os.name == "nt":
            parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            parent = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        value = parent / "FixLoop" / "state"
    return trusted_state_root(root, value, create=create)


def managed_state_base(root: str | Path, explicit: str | Path | None = None) -> Path:
    """Project partition under shared storage, including explicit/environment roots."""
    storage = managed_storage_root(root, explicit)
    return trusted_state_root(root, storage / "projects" / project_id(root))


def trusted_state_root(
    root: str | Path, explicit: str | Path | None = None, *, create: bool = True,
) -> Path | None:
    """Return the external state root only for an explicitly configured sandbox."""
    value = explicit or os.environ.get("FIXLOOP_STATE_ROOT", "")
    if not value:
        return None
    workspace = Path(root).resolve()
    state = Path(value).expanduser().resolve()
    if state == workspace or workspace in state.parents:
        raise ValueError("workspace_mapping_rejected: state_root inside workspace")
    if create:
        native_io_path(state).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not native_io_path(state).is_dir():
            raise ValueError("workspace_mapping_rejected: invalid state_root")
        # Windows non-strict resolution can retain an extended prefix when a
        # shared parent appears between probes. Resolve the original spelling
        # again after creation, before this path becomes durable identity.
        state = Path(value).expanduser().resolve()
        if state == workspace or workspace in state.parents:
            raise ValueError("workspace_mapping_rejected: state_root inside workspace")
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
    """Adapt a long Win32 file path at a known I/O boundary, never as identity.

    Short paths (including relative and UNC paths) retain their spelling. 248
    reserves the Win32 directory-creation suffix; callers must adapt child paths
    separately rather than store the returned spelling as a logical root.
    """
    value = Path(path)
    if os.name != "nt":
        return value
    spelling = str(value)
    if spelling.startswith(("\\\\?\\", "\\\\.\\")):
        return value
    raw = os.path.abspath(spelling)
    if max(len(spelling), len(raw)) < 248:
        return value
    # Extended paths disable Win32 trailing-dot/space normalization. Refuse to
    # silently address a different file when a caller supplied an ambiguous name.
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    devices.update(prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³")
    if any(
        part not in {".", ".."}
        and (part.endswith((".", " ")) or part.split(".")[0].rstrip(" ").upper() in devices)
        for part in value.parts
    ):
        raise ValueError("ambiguous Windows path cannot use extended I/O spelling")
    if len(raw) < 248:
        return value
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
    "managed_storage_root",
    "native_io_path",
    "project_id",
    "runtime_identity",
    "state_root_for",
    "trusted_state_root",
    "workspace_id",
]
