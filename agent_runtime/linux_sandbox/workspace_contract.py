"""Fixed workspace operations; no import, executable or host path supplied by a model."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

WORKSPACE_READ_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "grep",
        "ast_parse",
        "inspect_file",
        "find_test",
        "workspace_metadata",
        "workspace_source_version",
        "workspace_inventory",
        "workspace_snapshot",
        "workspace_environment",
        "workspace_text",
    }
)
WORKSPACE_WRITE_TOOLS = frozenset({"write_file", "patch_file", "apply_patch", "workspace_restore"})
WORKSPACE_TOOLS = WORKSPACE_READ_TOOLS | WORKSPACE_WRITE_TOOLS
MAX_REQUEST_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
_PROTECTED = frozenset({".git", ".agent", ".aws", ".ssh", ".gnupg"})


def _json_bytes(raw, error: str) -> bytes:
    try:
        return json.dumps(raw, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(error) from exc


def relative_path(raw: str, *, allow_root: bool = False) -> str:
    """Lexical validation only; the isolated filesystem remains the authority."""
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
        raise ValueError("workspace_mapping_rejected: invalid relative path")
    value = raw.replace("\\", "/")
    path = PurePosixPath(value)
    if value == "." and allow_root:
        return value
    if path.is_absolute() or any(
        part in {"", ".", ".."}
        or any(c in part for c in ':*?"<>|')
        or any(ord(c) < 32 for c in part)
        or part.endswith((".", " "))
        or part.split(".")[0].casefold() in _RESERVED
        for part in value.split("/")
    ):
        raise ValueError("workspace_mapping_rejected: relative paths required")
    from agent_runtime.sensitive_paths import is_sensitive_path

    if is_sensitive_path(value) or any(
        part.casefold() in _PROTECTED
        or part.casefold().startswith((".fixloop-s0-private-", ".fixloop-private-"))
        or is_sensitive_path(part)
        for part in path.parts
    ):
        raise ValueError("sensitive_path: protected workspace path")
    return path.as_posix()


def validate_versions(versions: dict) -> None:
    if not isinstance(versions, dict) or len(versions) > 128:
        raise ValueError("policy_denied: invalid base versions")
    for path, version in versions.items():
        if relative_path(path) != path or (
            version is not None and (not isinstance(version, str) or not _SHA256.fullmatch(version))
        ):
            raise ValueError("policy_denied: invalid base version")


def validate_workspace_request(raw: dict) -> None:
    if (
        raw.get("schema_version") != "2"
        or raw.get("argv") not in ([], ())
        or raw.get("cwd_relative") != "."
        or not isinstance(raw.get("tool_name"), str)
        or raw["tool_name"] not in WORKSPACE_TOOLS
        or not isinstance(raw.get("tool_args"), dict)
    ):
        raise ValueError("policy_denied: invalid workspace request")
    validate_versions(raw.get("base_versions", {}))
    if raw["tool_name"] in WORKSPACE_READ_TOOLS and raw.get("base_versions", {}):
        raise ValueError("policy_denied: read request carries mutation versions")
    payload = _json_bytes(raw, "policy_denied: non-JSON workspace request")
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("policy_denied: workspace request too large")


def tool_result_to_wire(result) -> dict:
    from dataclasses import asdict

    result.validate()
    raw = asdict(result)
    if len(_json_bytes(raw, "workspace_result_invalid: non-JSON result")) > MAX_RESULT_BYTES:
        from agent_runtime.tool_result import ToolResult

        return asdict(
            ToolResult.error("Error: workspace result too large", code="output_too_large")
        )
    return raw


def tool_result_from_wire(raw: dict):
    from agent_runtime.tool_result import ToolResult

    if not isinstance(raw, dict) or set(raw) != set(ToolResult.__dataclass_fields__):
        raise ValueError("workspace_result_invalid: result fields")
    if (
        not isinstance(raw["content"], str)
        or not isinstance(raw["metadata"], dict)
        or not isinstance(raw["changed_files"], list)
        or not all(isinstance(path, str) for path in raw["changed_files"])
        or not isinstance(raw["receipt"], dict)
        or not isinstance(raw["error_code"], str)
        or type(raw["retryable"]) is not bool
        or type(raw["output_truncated"]) is not bool
        or type(raw["duration_ms"]) is not int
        or raw["duration_ms"] < 0
    ):
        raise ValueError("workspace_result_invalid: field types")
    if len(_json_bytes(raw, "workspace_result_invalid: non-JSON result")) > MAX_RESULT_BYTES:
        raise ValueError("workspace_result_invalid: output limit")
    try:
        return ToolResult(**raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("workspace_result_invalid: result contract") from exc
