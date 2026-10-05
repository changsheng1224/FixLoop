"""Canonical Tool result and error contracts.

Every tool returns explicit status, retryability and side-effect metadata.
Text content is presentation only and never determines execution status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

RECEIPT_SCHEMA_VERSION = "1"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    NO_CHANGE = "no_change"
    REJECTED = "rejected"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    DRY_RUN = "dry_run"
    PARTIAL = "partial"


class ToolErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    PERMISSION_DENIED = "permission_denied"
    POLICY_DENIED = "policy_denied"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    SENSITIVE_PATH = "sensitive_path"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_CANCELLED = "tool_cancelled"
    DUPLICATE_CALL = "duplicate_call"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OUTPUT_TOO_LARGE = "output_too_large"
    STALE_PRECONDITION = "stale_precondition"
    STALE_PREIMAGE = "stale_preimage"
    NO_CHANGE = "no_change"
    MCP_UNAVAILABLE = "mcp_unavailable"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    PROVIDER_PROTOCOL_ERROR = "provider_protocol_error"
    UNKNOWN = "unknown"
    PARTIAL_RESULT = "partial_result"
    CLEANUP_UNVERIFIED = "cleanup_unverified"


@dataclass
class ToolResult:
    """Provider-neutral result consumed by AgentLoop and Observation Store."""

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = ToolStatus.SUCCESS
    error_code: str = ""
    retryable: bool | None = None
    data: Any = None
    changed_files: list[str] = field(default_factory=list)
    receipt: dict[str, Any] = field(default_factory=dict)
    output_truncated: bool = False
    duration_ms: int = 0

    def __post_init__(self) -> None:
        self.metadata = dict(self.metadata)
        self.status = ToolStatus(self.status)
        if self.retryable is None:
            self.retryable = self.status in {ToolStatus.ERROR, ToolStatus.REJECTED}
        self.validate()

    def validate(self) -> None:
        ToolStatus(self.status)
        if not isinstance(self.retryable, bool):
            raise TypeError("ToolResult.retryable must be a bool")
        reserved = {
            "tool_status",
            "tool_error_code",
            "retryable",
            "affected_paths",
            "receipt",
            "duration_ms",
            "output_truncated",
        }.intersection(self.metadata)
        if reserved:
            raise ValueError(f"Use ToolResult fields instead of metadata: {sorted(reserved)}")

    def to_metadata(self) -> dict[str, Any]:
        """Export a detached observation/trace snapshot from canonical fields."""
        from copy import deepcopy

        self.validate()
        return deepcopy(
            {
                **self.metadata,
                "tool_status": str(self.status),
                "tool_error_code": self.error_code,
                "retryable": self.retryable,
                "affected_paths": self.changed_files,
                "receipt": self.receipt,
                "duration_ms": self.duration_ms,
                "output_truncated": self.output_truncated,
            }
        )

    @classmethod
    def error(cls, content: str, *, code: str = "tool_execution_failed", retryable: bool = True):
        return cls(content=content, status=ToolStatus.ERROR, error_code=code, retryable=retryable)

    @property
    def ok(self) -> bool:
        return self.status in {ToolStatus.SUCCESS.value, ToolStatus.DRY_RUN.value}

    @property
    def failed(self) -> bool:
        return not self.ok


def require_tool_result(result: Any, *, tool_name: str = "") -> ToolResult:
    """Reject untyped tool implementations instead of guessing from text."""
    if not isinstance(result, ToolResult):
        raise TypeError(f"tool {tool_name!r} must return ToolResult, got {type(result).__name__}")
    result.validate()
    return result


def build_tool_receipt(
    tool_name: str,
    result: ToolResult,
    *,
    args_hash: str = "",
    run_id: str = "",
    call_id: str = "",
) -> dict[str, Any]:
    """Build a replay/audit receipt with stable fields for every tool call."""
    import hashlib
    import json

    changed = list(result.changed_files)
    body = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "tool": str(tool_name),
        "call_id": str(call_id or ""),
        "args_hash": str(args_hash or ""),
        "status": str(result.status),
        "error_code": str(result.error_code or ""),
        "retryable": bool(result.retryable),
        "duration_ms": int(result.duration_ms),
        "affected_paths": changed,
        "run_id": str(run_id or ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:20]
    body["receipt_id"] = "receipt-" + fingerprint
    return body


def attach_tool_receipt(
    result: Any,
    tool_name: str,
    *,
    args_hash: str = "",
    run_id: str = "",
    call_id: str = "",
) -> ToolResult:
    """Validate a tool result and attach its canonical audit receipt.

    Receipt assembly is deliberately kept at the result boundary so the
    executor and loop cannot diverge in status, metadata, or receipt fields.
    """
    normalized = require_tool_result(result, tool_name=tool_name)
    receipt = build_tool_receipt(
        tool_name,
        normalized,
        args_hash=args_hash,
        run_id=run_id,
        call_id=call_id,
    )
    normalized.receipt = receipt
    return normalized


__all__ = [
    "ToolErrorCode",
    "ToolResult",
    "ToolStatus",
    "RECEIPT_SCHEMA_VERSION",
    "attach_tool_receipt",
    "build_tool_receipt",
    "require_tool_result",
]
