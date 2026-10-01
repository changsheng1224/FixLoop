"""Controller/supervisor wire contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True)
class SandboxRequest:
    workspace_id: str
    task_id: str
    run_id: str
    call_id: str
    operation: Literal["command", "pytest"]
    argv: tuple[str, ...]
    cwd_relative: str = "."
    timeout_s: float = 20
    output_limit_bytes: int = 1048576
    schema_version: str = "1"
    owner_token: str = ""
    generation: int = 0
    coordination_revision: int = 0

    def to_wire(self) -> dict:
        return {**asdict(self), "argv": list(self.argv)}

    @classmethod
    def from_wire(cls, raw: dict) -> SandboxRequest:
        required = {
            "workspace_id",
            "task_id",
            "run_id",
            "call_id",
            "operation",
            "argv",
            "cwd_relative",
            "timeout_s",
            "output_limit_bytes",
            "schema_version",
        }
        if (
            not isinstance(raw, dict)
            or not required.issubset(raw)
            or set(raw) - set(cls.__dataclass_fields__)
        ):
            raise ValueError("invalid request fields")
        argv = raw["argv"]
        if (
            raw["schema_version"] != "1"
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item and "\x00" not in item for item in argv)
        ):
            raise ValueError("invalid request")
        return cls(**{**raw, "argv": tuple(argv)})


@dataclass(frozen=True)
class SandboxResult:
    execution_status: str
    exit_code: int | None = None
    error_code: str = ""
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    output_truncated: bool = False
    cleanup: str = "unverified"
    receipt_id: str = ""
    policy_digest: str = ""
    duration_ms: int = 0
    startup_ms: int = 0
    cleanup_ms: int = 0
    requested_backend: str = "wsl_bwrap"
    actual_backend: str = "none"
    mutation_status: str = "unknown"
    owner_token: str = ""
    generation: int = 0
    coordination_revision: int = 0

    def to_wire(self) -> dict:
        return asdict(self)
