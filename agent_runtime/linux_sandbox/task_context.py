"""Trusted task bindings. Mapping metadata does not authorize a runtime profile."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from agent_runtime.state_root import (
    managed_state_base,
    native_io_path,
    project_id,
    trusted_state_root,
    workspace_id,
)

_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")


def checked_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _ID.fullmatch(value)
        or value.endswith(".")
        or value.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    ):
        raise ValueError("sandbox_task_invalid: identifier")
    return value


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def windows_to_wsl(raw: str) -> str:
    """Drive-path source only; the target sees a guarded view at /workspace."""
    path = PureWindowsPath(raw)
    if (
        not path.is_absolute()
        or not re.fullmatch(r"[A-Za-z]:", path.drive)
        or "\x00" in raw
        or any(
            part in {"..", "."} or ":" in part or part.endswith((".", " "))
            for part in path.parts[1:]
        )
    ):
        raise ValueError("workspace_mapping_rejected: unsupported Windows path")
    return str(PurePosixPath("/mnt", path.drive[0].lower(), *path.parts[1:]))


@dataclass(frozen=True)
class WorkspaceMapping:
    host_root: str
    wsl_source: str
    sandbox_root: str
    filesystem_identity: tuple[int, int]
    workspace_id: str
    mapping_id: str
    profile: str

    def __post_init__(self):
        if (
            not all(
                isinstance(value, str) and value
                for value in (self.host_root, self.wsl_source, self.workspace_id, self.mapping_id)
            )
            or self.sandbox_root != "/workspace"
            or self.profile not in {"windows_guarded", "linux_native"}
            or not isinstance(self.filesystem_identity, tuple)
            or len(self.filesystem_identity) != 2
            or not all(type(value) is int for value in self.filesystem_identity)
        ):
            raise ValueError("workspace_mapping_rejected: invalid descriptor")

    @classmethod
    def create(cls, root: str | Path) -> WorkspaceMapping:
        supplied = Path(root).absolute()
        # A root alias must not silently change which directory the task owns.
        if supplied.is_symlink() or getattr(supplied, "is_junction", lambda: False)():
            raise ValueError("workspace_mapping_rejected: root alias")
        path = supplied.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("workspace_mapping_rejected: not a directory")
        stat = path.stat()
        host = str(path)
        source = windows_to_wsl(host) if os.name == "nt" else path.as_posix()
        profile = "windows_guarded" if os.name == "nt" else "linux_native"
        identity = (stat.st_dev, stat.st_ino)
        descriptor = {
            "host": os.path.normcase(host),
            "source": source,
            "target": "/workspace",
            "identity": identity,
            "profile": profile,
        }
        return cls(
            host, source, "/workspace", identity, workspace_id(path), _digest(descriptor), profile
        )

    def validate(self) -> None:
        if WorkspaceMapping.create(self.host_root) != self:
            raise ValueError("workspace_mapping_rejected: workspace identity changed")

    def sandbox_path(self, relative: str) -> str:
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise ValueError("workspace_mapping_rejected: relative path")
        path = PurePosixPath(relative.replace("\\", "/"))
        if path.is_absolute() or any(
            p == ".." or ":" in p or p.endswith((".", " ")) for p in path.parts
        ):
            raise ValueError("workspace_mapping_rejected: relative path")
        return str(PurePosixPath(self.sandbox_root) / path)


@dataclass(frozen=True)
class SandboxEnvironmentIdentity:
    distribution: str
    distribution_version: str
    runtime_version: str
    runtime_digest: str
    backend: str = "wsl_bwrap"

    def __post_init__(self):
        if (
            self.backend != "wsl_bwrap"
            or not all(
                isinstance(value, str) and value.strip() and "\x00" not in value
                for value in (self.distribution, self.distribution_version, self.runtime_version)
            )
            or not isinstance(self.runtime_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", self.runtime_digest)
        ):
            raise ValueError("sandbox_environment_invalid")

    @property
    def identity_id(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class SandboxTaskSpec:
    environment: SandboxEnvironmentIdentity
    policy_version: str
    policy_digest: str
    state_base: str = ""  # Shared storage root, before project partitioning.
    # Trusted source registration, never inferred from a temporary execution copy.
    project_root: str = ""

    def __post_init__(self):
        checked_id(self.policy_version)
        if (
            not isinstance(self.environment, SandboxEnvironmentIdentity)
            or not isinstance(self.state_base, str)
            or not isinstance(self.project_root, str)
            or not isinstance(self.policy_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", self.policy_digest)
        ):
            raise ValueError("sandbox_policy_invalid")


@dataclass(frozen=True)
class SandboxTaskBinding:
    task_id: str
    run_id: str
    repository_root: str
    mapping: WorkspaceMapping
    environment: SandboxEnvironmentIdentity
    policy_version: str
    policy_digest: str
    state_root: str
    project_root: str
    project_id: str
    project_directory_identity: tuple[int, int]
    workspace_ownership: str = "user"
    worktree_branch: str = ""
    schema_version: int = 2

    def __post_init__(self):
        checked_id(self.task_id)
        checked_id(self.run_id)
        if (
            self.run_id != self.task_id
            or type(self.schema_version) is not int
            or self.schema_version != 2
            or not isinstance(self.mapping, WorkspaceMapping)
            or not all(
                isinstance(value, str) and value
                for value in (
                    self.repository_root, self.state_root, self.project_root, self.project_id,
                )
            )
            or not isinstance(self.project_directory_identity, tuple)
            or len(self.project_directory_identity) != 2
            or not all(type(value) is int for value in self.project_directory_identity)
            or not isinstance(self.worktree_branch, str)
            or self.workspace_ownership not in {"user", "task"}
            or (self.workspace_ownership == "task" and not self.worktree_branch)
        ):
            raise ValueError("sandbox_task_invalid: binding")

    @classmethod
    def create(
        cls,
        repository: str | Path,
        workspace: str | Path,
        task_id: str,
        spec: SandboxTaskSpec,
        *,
        ownership: str = "user",
        branch: str = "",
    ):
        checked_id(task_id)
        mapping = WorkspaceMapping.create(workspace)
        source = WorkspaceMapping.create(spec.project_root or repository)
        base = managed_state_base(source.host_root, spec.state_base or None)
        state = trusted_state_root(mapping.host_root, base / "tasks" / task_id)
        binding = cls(
            task_id,
            task_id,
            str(Path(repository).resolve(strict=True)),
            mapping,
            spec.environment,
            spec.policy_version,
            spec.policy_digest,
            str(state),
            source.host_root,
            project_id(source.host_root),
            source.filesystem_identity,
            ownership,
            branch,
        )
        binding.validate()
        return binding

    @property
    def identity(self) -> dict:
        return {
            "backend": self.environment.backend,
            "policy_digest": self.policy_digest,
            "policy_version": self.policy_version,
            "distribution_id": self.environment.identity_id,
            "mapping_id": self.mapping.mapping_id,
            "project_id": self.project_id,
            "workspace_id": self.mapping.workspace_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "state_root": self.state_root,
            "workspace_ownership": self.workspace_ownership,
            "worktree_branch": self.worktree_branch,
            "receipt_id": "",
            "receipt_checksum": "",
        }

    def validate(self) -> None:
        checked_id(self.task_id)
        checked_id(self.run_id)
        if self.schema_version != 2 or self.workspace_ownership not in {"user", "task"}:
            raise ValueError("sandbox_task_invalid: binding")
        SandboxTaskSpec(self.environment, self.policy_version, self.policy_digest)
        self.mapping.validate()
        source = WorkspaceMapping.create(self.project_root)
        if (
            source.host_root != self.project_root
            or self.project_id != project_id(source.host_root)
            or source.filesystem_identity != self.project_directory_identity
        ):
            raise ValueError("sandbox_task_identity_mismatch: project identity changed")
        trusted_state_root(self.mapping.host_root, self.state_root)
        trusted_state_root(self.repository_root, self.state_root)
        trusted_state_root(self.project_root, self.state_root)

    def to_wire(self) -> dict:
        raw = asdict(self)
        raw["mapping"]["filesystem_identity"] = list(self.mapping.filesystem_identity)
        raw["project_directory_identity"] = list(self.project_directory_identity)
        return raw

    def save(self) -> Path:
        """Exclusive creation: never replace the authority of an existing task."""
        self.validate()
        path = Path(self.state_root) / "sandbox-task.json"
        with native_io_path(path).open("x", encoding="utf-8") as stream:
            raw = self.to_wire()
            json.dump({**raw, "checksum": _digest(raw)}, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    @classmethod
    def load_record(cls, path):
        """Load a durable binding for resource cleanup, without acquiring a run."""
        path = Path(path)
        raw = json.loads(native_io_path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.pop("checksum", None) != _digest(raw):
            raise ValueError("sandbox binding checksum invalid")
        mapping = raw.pop("mapping")
        environment = SandboxEnvironmentIdentity(**raw.pop("environment"))
        binding = cls(
            **{**raw, "project_directory_identity": tuple(raw["project_directory_identity"])},
            mapping=WorkspaceMapping(
                **{**mapping, "filesystem_identity": tuple(mapping["filesystem_identity"])}
            ),
            environment=environment,
        )
        if Path(binding.state_root).resolve() != path.parent.resolve():
            raise ValueError("sandbox binding state root mismatch")
        binding.validate()
        return binding

    @classmethod
    def load(cls, repository: str | Path, task_id: str, spec: SandboxTaskSpec):
        checked_id(task_id)
        source = WorkspaceMapping.create(spec.project_root or repository)
        base = managed_state_base(source.host_root, spec.state_base or None)
        state = trusted_state_root(repository, base / "tasks" / task_id)
        try:
            raw = json.loads(
                native_io_path(state / "sandbox-task.json").read_text(encoding="utf-8")
            )
            if not isinstance(raw, dict) or raw.pop("checksum", None) != _digest(raw):
                raise ValueError("binding checksum")
            mapping_raw = raw.pop("mapping")
            mapping = WorkspaceMapping(
                **{**mapping_raw, "filesystem_identity": tuple(mapping_raw["filesystem_identity"])}
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ValueError("sandbox_task_unavailable") from exc
        try:
            environment = SandboxEnvironmentIdentity(**raw.pop("environment"))
            binding = cls(
                **{**raw, "project_directory_identity": tuple(raw["project_directory_identity"])},
                mapping=mapping, environment=environment,
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ValueError("sandbox_task_invalid") from exc
        if (
            binding.task_id != task_id
            or binding.run_id != task_id
            or binding.repository_root != str(Path(repository).resolve(strict=True))
            or binding.project_root != source.host_root
            or binding.project_id != project_id(source.host_root)
            or binding.project_directory_identity != source.filesystem_identity
            or binding.state_root != str(state)
            or binding.environment != spec.environment
            or binding.policy_version != spec.policy_version
            or binding.policy_digest != spec.policy_digest
        ):
            raise ValueError("sandbox_task_identity_mismatch")
        binding.validate()
        return binding

    def attach(self, context) -> None:
        self.validate()
        if Path(context.root).resolve() != Path(self.mapping.host_root):
            raise ValueError("workspace_mapping_rejected: context root")
        context.sandbox_task_binding = self
        context.state_root = self.state_root
        context.sandbox_project_id = self.project_id
        context.sandbox_workspace_id = self.mapping.workspace_id
        context.sandbox_task_id = self.task_id
        context.sandbox_run_id = self.run_id
        context.sandbox_identity = self.identity

    def assert_context(self, context) -> None:
        self.validate()
        if (
            Path(context.root).resolve() != Path(self.mapping.host_root)
            or context.state_root != self.state_root
            or context.sandbox_project_id != self.project_id
            or context.sandbox_workspace_id != self.mapping.workspace_id
            or context.sandbox_task_id != self.task_id
            or context.sandbox_run_id != self.run_id
            or not isinstance(context.sandbox_identity, dict)
            or any(
                context.sandbox_identity.get(key) != value
                for key, value in self.identity.items()
                if key not in {"receipt_id", "receipt_checksum"}
            )
        ):
            raise ValueError("sandbox_task_identity_mismatch")
