"""Canonical Tool result and error contracts.

The runtime historically accepted arbitrary strings from tools.  This module
keeps that API compatible while giving every execution a typed status,
retryability and side-effect metadata that can be persisted and replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

RECEIPT_SCHEMA_VERSION = "1"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    REJECTED = "rejected"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    DRY_RUN = "dry_run"


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
    MCP_UNAVAILABLE = "mcp_unavailable"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    PROVIDER_PROTOCOL_ERROR = "provider_protocol_error"
    UNKNOWN = "unknown"


@dataclass
class ToolResult:
    """Provider-neutral result consumed by AgentLoop and Observation Store."""

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    error_code: str = ""
    retryable: bool | None = None
    data: Any = None
    changed_files: list[str] = field(default_factory=list)
    receipt: dict[str, Any] = field(default_factory=dict)
    output_truncated: bool = False
    duration_ms: int = 0

    def __post_init__(self) -> None:
        self.metadata = dict(self.metadata or {})
        metadata_status = str(self.metadata.get("tool_status", "") or "")
        if not self.status:
            self.status = metadata_status or ToolStatus.SUCCESS.value
        else:
            self.status = str(self.status)
        self.metadata["tool_status"] = self.status
        if not self.error_code:
            self.error_code = str(self.metadata.get("tool_error_code", "") or "")
        if self.error_code:
            self.metadata["tool_error_code"] = self.error_code
        if self.retryable is None:
            # Preserve the historical observation contract: unclassified
            # validation/execution failures are retryable by default. Callers
            # that must suppress replay can provide ``retryable=False`` in
            # metadata explicitly (budget, policy and idempotency rejections
            # do this at their boundary).
            self.retryable = bool(
                self.metadata.get(
                    "retryable",
                    self.status in {ToolStatus.ERROR.value, ToolStatus.REJECTED.value},
                )
            )
        else:
            self.retryable = bool(self.retryable)
        self.metadata.setdefault("retryable", self.retryable)
        if self.changed_files:
            self.metadata.setdefault("affected_paths", list(self.changed_files))
        elif self.metadata.get("affected_paths"):
            self.changed_files = list(self.metadata["affected_paths"])
        if self.receipt:
            self.metadata.setdefault("receipt", dict(self.receipt))
        elif isinstance(self.metadata.get("receipt"), dict):
            self.receipt = dict(self.metadata["receipt"])

    @property
    def ok(self) -> bool:
        return self.status in {ToolStatus.SUCCESS.value, ToolStatus.DRY_RUN.value}

    @property
    def failed(self) -> bool:
        return not self.ok


def normalize_tool_result(result: Any, *, tool_name: str = "") -> ToolResult:
    """Normalize legacy strings/MCP objects into the canonical result."""
    if isinstance(result, ToolResult):
        return result
    if hasattr(result, "content") and hasattr(result, "metadata"):
        metadata = dict(getattr(result, "metadata", {}) or {})
        return ToolResult(
            content=str(getattr(result, "content", "")),
            metadata=metadata,
            data=getattr(result, "data", None),
            receipt=dict(getattr(result, "receipt", {}) or {}),
        )
    text = str(result if result is not None else "")
    if text.lstrip().startswith("Error"):
        return ToolResult(
            content=text,
            status=ToolStatus.ERROR.value,
            error_code=ToolErrorCode.TOOL_EXECUTION_FAILED.value,
            retryable=True,
        )
    return ToolResult(content=text, status=ToolStatus.SUCCESS.value)


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

    changed = list(result.changed_files or result.metadata.get("affected_paths") or [])
    body = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "tool": str(tool_name),
        "call_id": str(call_id or ""),
        "args_hash": str(args_hash or ""),
        "status": str(result.status),
        "error_code": str(result.error_code or ""),
        "retryable": bool(result.retryable),
        "duration_ms": int(result.duration_ms or result.metadata.get("duration_ms", 0) or 0),
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
    """Normalize a tool result and attach its canonical audit receipt.

    Receipt assembly is deliberately kept at the result boundary so the
    executor and loop cannot diverge in status, metadata, or receipt fields.
    """
    normalized = normalize_tool_result(result, tool_name=tool_name)
    receipt = build_tool_receipt(
        tool_name,
        normalized,
        args_hash=args_hash,
        run_id=run_id,
        call_id=call_id,
    )
    normalized.receipt = receipt
    normalized.metadata["receipt"] = receipt
    return normalized


def result_metadata(result: Any) -> dict[str, Any]:
    """Compatibility helper for callers that only need a metadata projection."""
    return normalize_tool_result(result).metadata


__all__ = [
    "ToolErrorCode",
    "ToolResult",
    "ToolStatus",
    "RECEIPT_SCHEMA_VERSION",
    "attach_tool_receipt",
    "build_tool_receipt",
    "normalize_tool_result",
    "result_metadata",
]
