"""Controller/supervisor wire contract."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass(frozen=True)
class SandboxRequest:
    workspace_id: str
    task_id: str
    run_id: str
    call_id: str
    operation: Literal["command", "pytest", "workspace"]
    argv: tuple[str, ...]
    cwd_relative: str = "."
    timeout_s: float = 20
    output_limit_bytes: int = 1048576
    schema_version: str = "1"
    owner_token: str = ""
    generation: int = 0
    coordination_revision: int = 0
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    base_versions: dict[str, str | None] = field(default_factory=dict)

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
            raw["schema_version"] not in ("1", "2")
            or raw["operation"] not in ("command", "pytest", "workspace")
            or not isinstance(argv, list)
            or not all(isinstance(item, str) and item and "\x00" not in item for item in argv)
        ):
            raise ValueError("invalid request")
        if (
            any(
                not isinstance(raw[key], str)
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", raw[key])
                for key in ("workspace_id", "task_id", "run_id", "call_id")
            )
            or not isinstance(raw["cwd_relative"], str)
            or type(raw["timeout_s"]) not in (int, float)
            or not math.isfinite(raw["timeout_s"])
            or raw["timeout_s"] <= 0
            or type(raw["output_limit_bytes"]) is not int
            or raw["output_limit_bytes"] <= 0
            or not isinstance(raw.get("owner_token", ""), str)
            or any(
                type(raw.get(key, 0)) is not int or raw.get(key, 0) < 0
                for key in ("generation", "coordination_revision")
            )
        ):
            raise ValueError("invalid request envelope")
        if raw["operation"] == "workspace":
            from .workspace_contract import validate_workspace_request

            validate_workspace_request(raw)
        elif (
            raw["operation"] not in ("command", "pytest")
            or raw["schema_version"] != "1"
            or not argv
            or raw.get("tool_name", "") != ""
            or raw.get("tool_args", {}) != {}
            or raw.get("base_versions", {}) != {}
        ):
            raise ValueError("invalid command request")
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
    tool_result: dict = field(default_factory=dict)
    permission: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return asdict(self)
