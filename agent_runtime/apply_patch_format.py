"""Explicit patch envelopes adapted to the unique text patch core."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ApplyPatchOp",
    "parse_apply_patch_text",
    "strip_fences",
]


@dataclass
class ApplyPatchOp:
    action: str  # update | add | delete
    path: str
    diff: str = ""
    plan: object = None


_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n([\s\S]*?)\n```\s*$", re.MULTILINE)


def strip_fences(text: str) -> str:
    """剥 markdown / heredoc 围栏，便于模型 freeform 输出。"""
    raw = (text or "").strip("\r\n")
    if not raw:
        return ""
    if not raw.startswith(("```", "<<")):
        return raw
    # 整段围栏
    m = _FENCE_RE.match(raw)
    if m:
        return m.group(1).strip("\r\n")
    # 去首尾 ``` 行
    lines = raw.replace("\r\n", "\n").split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    # heredoc 风格
    if lines and lines[0].strip().startswith("<<"):
        lines = lines[1:]
        if lines and lines[-1].strip() in (".", "EOF", "PATCH"):
            lines = lines[:-1]
    return "\n".join(lines).strip("\r\n")


def parse_apply_patch_text(text: str) -> list[ApplyPatchOp]:
    """Parse the explicit envelope; preserve every hunk content line."""
    body = strip_fences(text)
    if body.startswith(("--- ", "diff --git ")):
        from agent_runtime.patch_engine import parse_unified_patch

        return [
            ApplyPatchOp(file.action, file.path, plan=file.plan)
            for file in parse_unified_patch(body)
        ]
    from agent_runtime.patch_engine import _lines

    lines = _lines(body)
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("apply_patch: expected *** Begin Patch ... *** End Patch")
    ops, seen, i = [], set(), 1
    while i < len(lines) - 1:
        header = re.fullmatch(r"\*\*\* (Update|Add|Delete) File: (.+)", lines[i])
        if not header:
            raise ValueError("apply_patch: unexpected content or unsupported operation")
        action = {"Update": "update", "Add": "add", "Delete": "delete"}[header[1]]
        path = header[2].replace("\\", "/")
        if path.casefold() in seen:
            raise ValueError("apply_patch: duplicate file path")
        seen.add(path.casefold())
        i += 1
        content = []
        while i < len(lines) - 1 and not lines[i].startswith("*** "):
            content.append(lines[i])
            i += 1
        if action == "delete" and content:
            raise ValueError("apply_patch: Delete File has unexpected content")
        if action == "update" and not content:
            raise ValueError("apply_patch: empty Update File body")
        if action == "add":
            if any(not line.startswith("+") for line in content):
                raise ValueError(
                    "apply_patch: Add File lines must begin with +; "
                    "use unified diff for no-final-newline creation"
                )
            content = [line[1:] for line in content]
        ops.append(ApplyPatchOp(action, path, "\n".join(content) + ("\n" if content else "")))
    if not ops:
        raise ValueError("apply_patch: empty input")
    return ops
