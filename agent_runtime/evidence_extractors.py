"""Generic, tool-aware evidence extraction for governed Context projections."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

_PATH_LINE = re.compile(r"(?P<path>[A-Za-z0-9_.@+/-]+\.[A-Za-z0-9_]+)(?::(?P<line>\d+))?")
_TEST_FAILURE = re.compile(r"(?i)(?:FAILED|ERROR)\s+(?P<target>[^\s]+)")


def _paths(text: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for match in _PATH_LINE.finditer(text or ""):
        path = str(PurePosixPath(match.group("path")))
        line = int(match.group("line") or 0)
        key = (path, line)
        if key in seen or path.startswith(("http://", "https://")):
            continue
        seen.add(key)
        facts.append({"kind": "source", "path": path, "line": line})
    return facts[:30]


def extract_evidence(
    tool: str,
    args: dict[str, Any] | None,
    text: str,
    *,
    source_version: str = "",
) -> list[dict[str, Any]]:
    """Extract bounded facts without deciding a repair target or patch."""
    name = str(tool or "")
    body = str(text or "")
    facts = (
        _paths(body)
        if name in {"read_file", "search", "grep", "test", "run_shell", "patch_file"}
        else []
    )
    if name in {"test", "run_shell", "quick_test", "sandbox_test", "verify"}:
        for match in _TEST_FAILURE.finditer(body):
            facts.append({"kind": "verification_failure", "target": match.group("target")})
    if name in {"write_file", "patch_file", "apply_patch"}:
        for path in (args or {}).get("path", ""),:
            if path:
                facts.append({"kind": "changed_file", "path": str(path)})
    if source_version:
        for fact in facts:
            fact["source_version"] = source_version
    return facts[:50]


def evidence_provenance(
    tool: str, args: dict[str, Any] | None, *, source_version: str = ""
) -> dict[str, Any]:
    return {
        "extractor": f"tool:{str(tool or 'unknown')}",
        "extractor_version": "evidence-v1",
        "tool": str(tool or ""),
        "args": dict(args or {}),
        "source_version": str(source_version or ""),
    }
