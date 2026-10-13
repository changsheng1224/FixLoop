"""Pure UTF-8 text patch core. No filesystem, permissions or runtime dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class PatchError(ValueError):
    """Invalid format or a stale preimage; safe to report before any writes."""

    def __init__(self, message: str, code: str = "invalid_patch"):
        super().__init__(message)
        self.code = code


@dataclass
class PatchHunk:
    header: str
    lines: list[tuple[str, str]]
    no_newline: set[int] = field(default_factory=set)


@dataclass
class PatchPlan:
    mode: str
    hunks: list[PatchHunk] = field(default_factory=list)
    old_text: str = ""
    new_text: str = ""
    newline: str = "\n"


@dataclass
class FilePatch:
    path: str
    action: str
    plan: PatchPlan


@dataclass
class PatchPreview:
    path: str
    hunk_count: int
    lines_added: int
    lines_removed: int
    hunks: list[dict]


_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)")
_NO_NEWLINE = "\\ No newline at end of file"


def _lines(text: str, *, keepends: bool = False) -> list[str]:
    if "\x00" in text:
        raise PatchError("binary text is unsupported")
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or ("\r\n" in text and "\n" in text.replace("\r\n", "")):
        raise PatchError("mixed or CR-only newlines are unsupported")
    parts = normalized.split("\n")
    if keepends:
        return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    return parts[:-1] if normalized.endswith("\n") else parts if normalized else []


def _coordinates(header: str) -> tuple[int, int, int, int]:
    match = _HEADER.fullmatch(header)
    if match is None:
        raise PatchError(f"invalid hunk header: {header}")
    a, n, b, m = int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1)
    if (n and not a) or (m and not b):
        raise PatchError("nonempty hunk coordinates must start at one")
    return a - bool(n), n, b - bool(m), m


def _parse_hunks(lines: list[str], start: int = 0) -> tuple[list[PatchHunk], int]:
    hunks = []
    i = start
    while i < len(lines) and lines[i].startswith("@@"):
        header = lines[i]
        _, old_count, _, new_count = _coordinates(header)
        i += 1
        body, markers = [], set()
        old_used = new_used = 0
        while old_used < old_count or new_used < new_count:
            if i >= len(lines) or not lines[i] or lines[i][0] not in " +-":
                raise PatchError("hunk body does not match declared line counts")
            kind, text = lines[i][0], lines[i][1:]
            body.append((kind, text))
            old_used += kind in " -"
            new_used += kind in " +"
            if old_used > old_count or new_used > new_count:
                raise PatchError("hunk body exceeds declared line counts")
            i += 1
            if i < len(lines) and lines[i] == _NO_NEWLINE:
                markers.add(len(body) - 1)
                i += 1
        hunks.append(PatchHunk(header, body, markers))
    return hunks, i


def _path(header: str, prefix: str) -> str | None:
    value = header[4:].split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith(prefix):
        value = value[len(prefix) :]
    if not value or value.startswith(('"', "/")) or "\\" in value:
        raise PatchError("unsupported or unsafe file path")
    if any(part in {"", ".", ".."} for part in value.split("/")) or ":" in value:
        raise PatchError("unsafe file path")
    return value


def parse_unified_patch(diff: str) -> list[FilePatch]:
    """Parse multi-file unified diff; reject binary, rename and mode changes."""
    if not isinstance(diff, str) or not diff:
        raise PatchError("empty patch")
    lines, i, files, seen = _lines(diff), 0, [], set()
    while i < len(lines):
        while i < len(lines) and lines[i].startswith(("diff --git ", "index ")):
            i += 1
        # Creation/deletion metadata must still agree with /dev/null headers.
        metadata = ""
        if i < len(lines) and lines[i] in {"new file mode 100644", "deleted file mode 100644"}:
            metadata = lines[i].split()[0]
            i += 1
        if (
            i + 1 >= len(lines)
            or not lines[i].startswith("--- ")
            or not lines[i + 1].startswith("+++ ")
        ):
            raise PatchError("expected ---/+++ file headers; unsupported patch format")
        old, new = _path(lines[i], "a/"), _path(lines[i + 1], "b/")
        if old is None and new is None or old and new and old != new:
            raise PatchError("renames and two null paths are unsupported")
        action = "add" if old is None else "delete" if new is None else "update"
        if metadata and metadata != {"add": "new", "delete": "deleted"}.get(action):
            raise PatchError("file metadata disagrees with headers")
        path = new or old
        if path.casefold() in seen:
            raise PatchError(f"duplicate file patch: {path}")
        seen.add(path.casefold())
        hunks, i = _parse_hunks(lines, i + 2)
        if not hunks and action == "update":
            raise PatchError("file update requires hunks")
        files.append(
            FilePatch(
                path, action, PatchPlan("diff", hunks, newline="\r\n" if "\r\n" in diff else "\n")
            )
        )
    return files


def parse_patch_input(args: dict) -> PatchPlan:
    diff = args.get("diff")
    if diff:
        if not isinstance(diff, str):
            raise PatchError("diff must be text")
        if diff.startswith("@@"):
            hunks, end = _parse_hunks(_lines(diff))
            if end != len(_lines(diff)):
                raise PatchError("unexpected content after hunk")
            return PatchPlan("diff", hunks, newline="\r\n" if "\r\n" in diff else "\n")
        files = parse_unified_patch(diff)
        if len(files) != 1 or files[0].action != "update":
            raise PatchError("patch_file accepts one existing file; use a multi-file transaction")
        requested = args.get("path")
        if requested and requested.replace("\\", "/") != files[0].path:
            raise PatchError("patch path differs from tool target")
        return files[0].plan
    old, new = args.get("old_text"), args.get("new_text")
    if isinstance(old, str) and old and isinstance(new, str):
        return PatchPlan("replacement", old_text=old, new_text=new)
    raise PatchError("provide diff or nonempty old_text and new_text")


def apply_text(text: str, plan: PatchPlan) -> str:
    """Validate and construct output against the immutable original text."""
    if "\x00" in text:
        raise PatchError("binary text is unsupported")
    if plan.mode == "replacement":
        if "\x00" in plan.new_text:
            raise PatchError("binary text is unsupported")
        count = text.count(plan.old_text)
        if not plan.old_text or count != 1:
            raise PatchError(f"old_text 出现 {count} 次，必须恰好匹配 1 次", "stale_preimage")
        _lines(text)
        result = text.replace(plan.old_text, plan.new_text, 1)
        _lines(result)
        return result
    if plan.mode != "diff":
        raise PatchError(f"unsupported patch mode: {plan.mode}")
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or "\r\n" in text and "\n" in text.replace("\r\n", ""):
        raise PatchError("mixed or CR-only newlines are unsupported")
    newline = "\r\n" if "\r\n" in text else "\n" if "\n" in text else plan.newline
    original = _lines(text, keepends=True)
    output, cursor = [], 0
    for hunk in plan.hunks:
        start, old_count, new_start, new_count = _coordinates(hunk.header)
        if start < cursor or start > len(original) or start + old_count > len(original):
            raise PatchError("overlapping or out of range hunk", "stale_preimage")
        output.extend(original[cursor:start])
        if new_start != len(output):
            raise PatchError("inconsistent new hunk coordinates")
        cursor, old_used, new_used = start, 0, 0
        for index, (kind, content) in enumerate(hunk.lines):
            line = content + ("" if index in hunk.no_newline else "\n")
            if kind in " -":
                if cursor >= len(original) or original[cursor] != line:
                    raise PatchError(
                        f"hunk preimage mismatch at line {cursor + 1}", "stale_preimage"
                    )
                cursor += 1
                old_used += 1
            if kind in " +":
                output.append(line)
                new_used += 1
        if (old_used, new_used) != (old_count, new_count):
            raise PatchError("hunk line counts mismatch")
    output.extend(original[cursor:])
    if any(not line.endswith("\n") for line in output[:-1]):
        raise PatchError("no-newline marker may only terminate a file")
    return "".join(output).replace("\n", newline)


def apply_file_patches(
    sources: dict[str, str | None], files: list[FilePatch]
) -> dict[str, str | None]:
    output = {}
    for patch in files:
        source = sources[patch.path]
        if patch.path.casefold() in {p.casefold() for p in output}:
            raise PatchError("duplicate target")
        if patch.action == "add" and source is not None or patch.action != "add" and source is None:
            raise PatchError(f"file existence changed: {patch.path}", "stale_preimage")
        result = apply_text(source or "", patch.plan)
        if patch.action == "delete" and result:
            raise PatchError("delete patch must remove the entire file")
        output[patch.path] = None if patch.action == "delete" else result
    return output


def apply_operations(sources, operations) -> dict[str, str | None]:
    """Codex envelope adapter. All updates use the same validated plan."""
    files = []
    for op in operations:
        source = sources[op.path]
        if getattr(op, "plan", None) is not None:
            files.append(FilePatch(op.path, op.action, op.plan))
            continue
        if op.action == "update":
            if source is None:
                raise PatchError(f"file missing: {op.path}", "stale_preimage")
            plan = parse_patch_input({"diff": normalize_hunk_headers(op.diff, source)})
        elif op.action == "add":
            lines = _lines(op.diff)
            body = [("+", line) for line in lines]
            plan = PatchPlan("diff", [PatchHunk(f"@@ -0,0 +{bool(lines):d},{len(lines)} @@", body)])
        elif op.action == "delete":
            if source is None:
                raise PatchError(f"file missing: {op.path}", "stale_preimage")
            lines = _lines(source)
            markers = {len(lines) - 1} if source and not source.endswith("\n") else set()
            plan = PatchPlan(
                "diff",
                [
                    PatchHunk(
                        f"@@ -{bool(lines):d},{len(lines)} +0,0 @@",
                        [("-", line) for line in lines],
                        markers,
                    )
                ],
            )
        else:
            raise PatchError(f"unknown file operation: {op.action}")
        files.append(FilePatch(op.path, op.action, plan))
    return apply_file_patches(sources, files)


def build_preview(path: str, plan: PatchPlan) -> PatchPreview:
    """从补丁计划构建预览摘要。"""
    hunks_out: list[dict] = []
    lines_added = 0
    lines_removed = 0

    if plan.mode == "replacement":
        removed = plan.old_text.splitlines()
        added = plan.new_text.splitlines()
        lines_removed = len(removed)
        lines_added = len(added)
        hunks_out.append(
            {
                "header": "@@ replacement @@",
                "removed": removed[:20],
                "added": added[:20],
            }
        )
        return PatchPreview(
            path=path,
            hunk_count=1,
            lines_added=lines_added,
            lines_removed=lines_removed,
            hunks=hunks_out,
        )

    for hunk in plan.hunks:
        removed = [text for kind, text in hunk.lines if kind == "-"]
        added = [text for kind, text in hunk.lines if kind == "+"]
        lines_removed += len(removed)
        lines_added += len(added)
        hunks_out.append(
            {
                "header": hunk.header,
                "removed": removed[:20],
                "added": added[:20],
            }
        )

    return PatchPreview(
        path=path,
        hunk_count=len(plan.hunks),
        lines_added=lines_added,
        lines_removed=lines_removed,
        hunks=hunks_out,
    )


def format_preview_text(
    preview: PatchPreview,
    *,
    max_hunks: int = 3,
    max_lines: int = 8,
) -> str:
    """格式化为审批/UI 可读的 diff 摘要。"""
    header = (
        f"--- 预览 ({preview.hunk_count} hunk"
        f"{'' if preview.hunk_count == 1 else 's'}, "
        f"-{preview.lines_removed}/+{preview.lines_added} lines) ---"
    )
    parts = [header]
    for hunk in preview.hunks[:max_hunks]:
        parts.append(hunk["header"])
        shown = 0
        for line in hunk.get("removed", []):
            if shown >= max_lines:
                break
            parts.append(f"-{line}")
            shown += 1
        shown = 0
        for line in hunk.get("added", []):
            if shown >= max_lines:
                break
            parts.append(f"+{line}")
            shown += 1
    if preview.hunk_count > max_hunks:
        parts.append(f"... 另有 {preview.hunk_count - max_hunks} 个 hunk")
    return "\n".join(parts)


def preview_to_metadata(preview: PatchPreview) -> dict:
    """转为 trace / ToolResult metadata。"""
    return {
        "path": preview.path,
        "hunk_count": preview.hunk_count,
        "lines_added": preview.lines_added,
        "lines_removed": preview.lines_removed,
        "preview_text": format_preview_text(preview),
        "hunks": preview.hunks,
    }


def try_build_patch_preview(
    path: str, file_text: str, args: dict
) -> tuple[dict | None, str | None]:
    try:
        plan = parse_patch_input(args)
        apply_text(file_text, plan)
        return preview_to_metadata(build_preview(path, plan)), None
    except PatchError as exc:
        return None, str(exc)


def normalize_hunk_headers(diff: str, file_text: str) -> str:
    """Resolve Codex bare @@ from a unique exact preimage, inside the core."""
    lines = _lines(diff)
    if not lines or not lines[0].startswith("@@"):
        raise PatchError("update requires @@ hunks")
    original = _lines(file_text)
    result, i, cursor, delta = [], 0, 0, 0
    while i < len(lines):
        header = lines[i]
        i += 1
        body = []
        while i < len(lines) and not lines[i].startswith("@@"):
            body.append(lines[i])
            i += 1
        if _HEADER.fullmatch(header):
            start, n, _, m = _coordinates(header)
        else:
            if header != "@@" and not header.startswith("@@ "):
                raise PatchError("unsupported hunk header")
            preimage = [line[1:] for line in body if line and line[0] in " -"]
            if not preimage:
                raise PatchError("bare @@ requires a nonempty preimage")
            matches = [
                pos
                for pos in range(cursor, len(original) - len(preimage) + 1)
                if original[pos : pos + len(preimage)] == preimage
            ]
            if len(matches) != 1:
                raise PatchError("bare @@ preimage must match exactly once", "stale_preimage")
            start, n = matches[0], len(preimage)
            m = sum(bool(line) and line[0] in " +" for line in body)
            header = f"@@ -{start + bool(n)},{n} +{start + delta + bool(m)},{m} @@"
        cursor = start + n
        delta += m - n
        result.extend([header, *body])
    return "\n".join(result) + "\n"
