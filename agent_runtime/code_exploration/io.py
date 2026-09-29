"""Budgeted filesystem retrieval. Every byte counted here is actually read."""

from __future__ import annotations

import codecs
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.code_exploration.models import (
    RetrievalHit,
    RetrievalLimits,
    RetrievalResult,
)
from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolErrorCode, ToolResult

_IGNORED = frozenset(
    {
        ".git",
        ".agent",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "venv",
        ".venv",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".eggs",
    }
)
_CHUNK = 4096


@dataclass
class ScanState:
    bytes_read: int = 0
    lines_seen: int = 0
    eof: bool = False
    stop_reason: str = ""
    content_hash: str | None = None


def _limits(context: ToolContext) -> RetrievalLimits:
    configured = getattr(context, "exploration_limits", None)
    return configured if isinstance(configured, RetrievalLimits) else RetrievalLimits()


def _safe_path(context: ToolContext, raw: str) -> tuple[Path | None, str]:
    try:
        path = context.resolve(raw)
    except ValueError as exc:
        return None, f"Error: {exc}"
    if is_sensitive_path(raw) or is_sensitive_path(path):
        return None, sensitive_reject_message(raw)
    return path, ""


def _path_error_code(message: str) -> str:
    return (
        ToolErrorCode.SENSITIVE_PATH.value
        if "敏感路径" in message
        else ToolErrorCode.PATH_OUTSIDE_WORKSPACE.value
    )


def _relative(context: ToolContext, path: Path) -> str:
    return path.relative_to(Path(context.root).resolve()).as_posix()


def _stop_reason(context: ToolContext, deadline: float) -> str:
    token = getattr(context, "cancel_token", None)
    if token is not None and token.is_cancelled:
        return "cancelled"
    runtime_deadline = getattr(context, "deadline", None)
    if runtime_deadline is not None and hasattr(runtime_deadline, "remaining_s"):
        remaining = runtime_deadline.remaining_s()
        if remaining is not None and remaining <= 0:
            return "timeout"
    return "timeout" if time.monotonic() >= deadline else ""


def _lines(
    path: Path,
    *,
    byte_limit: int,
    line_limit: int,
    context: ToolContext,
    deadline: float,
    state: ScanState,
) -> Iterator[tuple[int, str, bool]]:
    """Read bounded pieces of lines; never allocate an unbounded line."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    parts: list[str] = []
    captured = 0
    long_line = False
    line_no = 1
    digest = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as stream:
        while state.bytes_read < byte_limit:
            reason = _stop_reason(context, deadline)
            if reason:
                state.stop_reason = reason
                break
            chunk = stream.readline(min(_CHUNK, byte_limit - state.bytes_read))
            if not chunk:
                state.eof = True
                break
            state.bytes_read += len(chunk)
            digest.update(chunk)
            if b"\x00" in chunk or (
                state.bytes_read == len(chunk)
                and chunk.startswith((b"\x7fELF", b"MZ", b"\x89PNG", b"PK\x03\x04"))
            ):
                state.stop_reason = "binary"
                break
            if stream.tell() >= size:
                state.eof = True
                state.content_hash = digest.hexdigest()
            end_line = chunk.endswith(b"\n")
            body = chunk[:-1] if end_line else chunk
            decoded = decoder.decode(body, final=end_line)
            if captured < line_limit:
                remaining = line_limit - captured
                encoded = decoded.encode("utf-8")
                kept = encoded[:remaining].decode("utf-8", errors="ignore")
                parts.append(kept)
                captured += len(kept.encode("utf-8"))
                long_line |= len(encoded) > remaining
            elif decoded:
                long_line = True
            if end_line:
                text = "".join(parts).rstrip("\r")
                state.lines_seen = line_no
                yield line_no, text, long_line
                line_no += 1
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                parts, captured, long_line = [], 0, False
        if stream.tell() >= size:
            state.eof = True
        if parts or long_line:
            state.lines_seen = line_no
            yield line_no, "".join(parts) + decoder.decode(b"", final=True), long_line
        if state.eof:
            state.content_hash = digest.hexdigest()
        elif not state.stop_reason:
            state.stop_reason = "scan_bytes"


def _result(query_type: str, started: float) -> RetrievalResult:
    result = RetrievalResult(query_type=query_type)
    result.budget_used = {"bytes_read": 0, "files_scanned": 0, "output_bytes": 0}
    result.duration_ms = round((time.monotonic() - started) * 1000)
    return result


def _finish(
    result: RetrievalResult,
    content: str,
    started: float,
    limits: RetrievalLimits,
    *,
    error_code: str = "",
) -> ToolResult:
    if result.execution != "ok" and result.completeness == "complete_in_scope":
        result.completeness = "unknown"
    encoded = content.encode("utf-8")
    if len(encoded) > limits.visible_bytes:
        content = encoded[: limits.visible_bytes].decode("utf-8", errors="ignore")
        result.partial("output_bytes")
    result.budget_used["output_bytes"] = len(content.encode("utf-8"))
    result.duration_ms = round((time.monotonic() - started) * 1000)
    tool_result = result.to_tool_result(content)
    if error_code:
        tool_result.error_code = error_code
        tool_result.metadata["tool_error_code"] = error_code
    return tool_result


def read_file_result(context: ToolContext, args: dict) -> ToolResult:
    started = time.monotonic()
    limits = _limits(context)
    result = _result("range_read", started)
    raw = str(args.get("path", ""))
    if not raw:
        result.execution = "rejected"
        return _finish(result, "Error: 缺少必填参数 path", started, limits)
    path, error = _safe_path(context, raw)
    if error:
        result.execution = "rejected"
        return _finish(result, error, started, limits, error_code=_path_error_code(error))
    if path is None or not path.is_file():
        result.execution = "error"
        return _finish(result, f"Error: 文件不存在: {raw}", started, limits)
    try:
        start = max(1, int(args.get("start", 1)))
        end = int(args.get("end", 200))
    except (TypeError, ValueError):
        result.execution = "rejected"
        return _finish(result, "Error: 无效的行号", started, limits)
    if end < start:
        result.execution = "rejected"
        return _finish(result, "Error: end 不能小于 start", started, limits)
    scan_limit = limits.lower(range_scan_bytes=args.get("max_scan_bytes", limits.range_scan_bytes))
    deadline = started + limits.timeout_s
    state = ScanState()
    output: list[str] = []
    excerpt = hashlib.sha256()
    returned_bytes = 0
    last_line = start - 1
    try:
        for line_no, text, long_line in _lines(
            path,
            byte_limit=scan_limit.range_scan_bytes,
            line_limit=limits.line_bytes,
            context=context,
            deadline=deadline,
            state=state,
        ):
            if line_no < start:
                continue
            if line_no > end or len(output) >= limits.range_return_lines:
                result.partial("return_lines")
                break
            line = f"{line_no:4d} | {text}"
            length = len(line.encode("utf-8"))
            if returned_bytes + length > limits.range_return_bytes:
                result.partial("return_bytes")
                break
            output.append(line)
            excerpt.update((text + "\n").encode("utf-8"))
            returned_bytes += length
            last_line = line_no
            if long_line:
                result.partial("long_line")
            if line_no >= end:
                break
    except OSError as exc:
        result.execution = "error"
        return _finish(result, f"Error: 无法读取文件: {exc}", started, limits)
    result.budget_used.update(bytes_read=state.bytes_read, files_scanned=1)
    result.scanned_scope = {"paths": [_relative(context, path)], "lines_seen": state.lines_seen}
    if state.stop_reason == "cancelled":
        result.execution = "cancelled"
        return _finish(result, "Error: 查询已取消", started, limits)
    if state.stop_reason == "binary":
        result.execution = "rejected"
        return _finish(
            result,
            f"Error: 疑似二进制文件，拒绝读取: {raw}",
            started,
            limits,
            error_code="binary_file",
        )
    if state.stop_reason:
        result.partial(state.stop_reason)
        if state.stop_reason == "timeout":
            result.execution = "timeout"
    if not output and state.eof and start > state.lines_seen:
        result.execution = "error"
        return _finish(
            result, f"Error: start({start}) 超出文件行数({state.lines_seen})", started, limits
        )
    rel = _relative(context, path)
    file_hash = (
        state.content_hash if state.eof and path.stat().st_size <= limits.file_hash_bytes else None
    )
    if file_hash:
        result.dependency_versions[rel] = file_hash
    if output:
        result.hits.append(
            RetrievalHit(
                hit_id=f"{result.query_id}:1",
                path=rel,
                range={
                    "start_line": start,
                    "start_column": None,
                    "end_line": last_line + 1,
                    "end_column": None,
                    "exact": False,
                },
                kind="source_excerpt",
                summary=f"lines {start}-{last_line}",
                source="filesystem",
                content_hash=file_hash,
                excerpt_hash=excerpt.hexdigest(),
            )
        )
    total = str(state.lines_seen) if state.eof else "?"
    header = f"# {raw}  ({start}-{last_line}/{total} 行)"
    return _finish(result, header + ("\n" + "\n".join(output) if output else ""), started, limits)


def _candidate_files(
    context: ToolContext,
    root: Path,
    *,
    glob: str,
    max_files: int,
    deadline: float,
) -> Iterator[Path | None]:
    if root.is_file():
        paths = [root]
        for path in paths:
            yield path
        return
    examined = 0
    for directory, names, files in os.walk(root, followlinks=False):
        if _stop_reason(context, deadline):
            return
        names[:] = sorted(
            name
            for name in names
            if name not in _IGNORED and not name.startswith(".") and not name.endswith(".egg-info")
        )
        for name in sorted(files):
            if _stop_reason(context, deadline):
                return
            if name in _IGNORED or name.startswith("."):
                continue
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(name, glob)):
                continue
            examined += 1
            if examined > max_files:
                yield None
                return
            checked, error = _safe_path(context, _relative(context, path))
            if error or checked is None or not checked.is_file():
                continue
            yield checked


def list_files_result(context: ToolContext, args: dict) -> ToolResult:
    started = time.monotonic()
    limits = _limits(context)
    result = _result("file_listing", started)
    raw = str(args.get("path", "."))
    root, error = _safe_path(context, raw)
    if error:
        result.execution = "rejected"
        return _finish(result, error, started, limits, error_code=_path_error_code(error))
    if root is None or not root.is_dir():
        result.execution = "error"
        return _finish(result, f"Error: 目录不存在: {raw}", started, limits)
    depth = min(10, max(0, int(args.get("depth", 1))))
    max_results = min(limits.max_hits, max(1, int(args.get("max_results", 200))))
    pattern = str(args.get("glob", "") or "")
    deadline = started + limits.timeout_s
    entries = []
    checked = 0
    for directory, names, files in os.walk(root, followlinks=False):
        reason = _stop_reason(context, deadline)
        if reason:
            result.partial(reason)
            result.execution = "cancelled" if reason == "cancelled" else "timeout"
            break
        relative_dir = Path(directory).relative_to(root)
        current_depth = len(relative_dir.parts) if relative_dir != Path(".") else 0
        names[:] = sorted(
            name
            for name in names
            if name not in _IGNORED and not name.startswith(".") and not name.endswith(".egg-info")
        )
        if depth == 1:
            candidates = [(Path(directory) / name, True) for name in names]
        else:
            candidates = []
        candidates.extend((Path(directory) / name, False) for name in sorted(files))
        for candidate, is_dir in candidates:
            if candidate.name.startswith("."):
                continue
            checked += 1
            if checked > limits.search_files:
                result.partial("candidate_files")
                break
            rel = candidate.relative_to(root).as_posix()
            if pattern and not (
                fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(candidate.name, pattern)
            ):
                continue
            safe, reject = _safe_path(context, _relative(context, candidate))
            if reject or safe is None:
                continue
            if not is_dir and current_depth + 1 > depth and depth != 0:
                continue
            if depth == 1 or not is_dir:
                entries.append(f"[{'D' if is_dir else 'F'}] {rel}")
                result.hits.append(
                    RetrievalHit(
                        hit_id=f"{result.query_id}:{len(result.hits) + 1}",
                        path=_relative(context, safe),
                        range=None,
                        kind="directory" if is_dir else "file",
                        summary=rel,
                        source="filesystem",
                    )
                )
            if len(entries) >= max_results:
                result.partial("results")
                break
        if result.truncation_reasons:
            break
        if depth == 1 or (depth and current_depth + 1 >= depth):
            names.clear()
    result.scanned_scope = {"root": _relative(context, root), "candidates_checked": checked}
    result.budget_used["files_scanned"] = checked
    if result.execution == "cancelled":
        return _finish(result, "Error: 查询已取消", started, limits)
    content = (
        "\n".join(entries)
        if entries
        else (f"(无匹配) {raw} glob={pattern!r}" if pattern else f"(空目录) {raw}")
    )
    if result.truncation_reasons:
        content += "\n(结果未完整显示)"
    return _finish(result, content, started, limits)


def _render_matches(rows: list[tuple[str, int, str]]) -> str:
    if not rows:
        return "(无匹配 / 0 matches — command succeeded with no output)"
    output = []
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and rows[j][0] == rows[i][0] and rows[j][1] == rows[j - 1][1] + 1:
            j += 1
        if j - i == 1:
            path, line, text = rows[i]
            output.append(f"{path}:{line}: {text.strip()}")
        else:
            output.append(f"{rows[i][0]}:{rows[i][1]}-{rows[j - 1][1]}:")
            output.extend(f"{line}: {text.strip()}" for _, line, text in rows[i:j])
        i = j
    return "\n".join(output)


def _rg_small_file(
    path: Path,
    *,
    executable: str,
    pattern: str,
    ignore_case: bool,
    context_lines: int,
    max_hits: int,
    deadline: float,
    context: ToolContext,
) -> tuple[list[tuple[int, str, bool]] | None, int, str | None, str]:
    """Run rg on a separately bounded input; rg's output is capped by its flags."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        data = stream.read(size + 1)
    if len(data) != size:
        return None, len(data), None, "file_changed"
    if b"\x00" in data:
        return [], len(data), None, "binary"
    command = [
        executable,
        "-n",
        "--no-heading",
        "--no-filename",
        "--color",
        "never",
        "--max-count",
        str(max_hits),
        "--max-columns",
        "160",
        "--max-columns-preview",
    ]
    if context_lines:
        command.extend(["-C", str(context_lines)])
    if ignore_case:
        command.append("-i")
    command.extend(["-e", pattern, "-"])
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        return None, len(data), None, "rg_unavailable"
    payload: bytes | None = data
    while True:
        reason = _stop_reason(context, deadline)
        if reason:
            process.kill()
            process.communicate()
            return None, len(data), None, reason
        try:
            stdout, _ = process.communicate(
                input=payload, timeout=min(0.2, max(0.01, deadline - time.monotonic()))
            )
            break
        except subprocess.TimeoutExpired:
            payload = None
    if process.returncode not in {0, 1}:
        return None, len(data), None, "rg_error"
    rows = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        match = re.match(r"^(\d+)([:\-])(.*)$", line)
        if match:
            rows.append((int(match.group(1)), match.group(3).strip()[:200], match.group(2) == ":"))
    return rows, len(data), hashlib.sha256(data).hexdigest(), ""


def grep_result(context: ToolContext, args: dict) -> ToolResult:
    started = time.monotonic()
    limits = _limits(context)
    result = _result("text_search", started)
    pattern = str(args.get("pattern", ""))
    if not pattern:
        result.execution = "rejected"
        return _finish(result, "Error: 缺少必填参数 pattern", started, limits)
    root, error = _safe_path(context, str(args.get("path", ".")))
    if error:
        result.execution = "rejected"
        return _finish(result, error, started, limits, error_code=_path_error_code(error))
    if root is None or not root.exists():
        result.execution = "error"
        return _finish(result, f"Error: 路径不存在: {args.get('path', '.')}", started, limits)
    try:
        regex = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE if args.get("ignore_case") else 0)
    max_hits = min(limits.max_hits, max(1, int(args.get("max_results", limits.max_hits))))
    max_files = min(limits.search_files, max(1, int(args.get("max_files", limits.search_files))))
    max_bytes = min(
        limits.search_read_bytes, max(1, int(args.get("max_read_bytes", limits.search_read_bytes)))
    )
    context_lines = min(3, max(0, int(args.get("context_lines", 0))))
    deadline = started + limits.timeout_s
    rows: dict[tuple[str, int], str] = {}
    files = 0
    bytes_read = 0
    scanned_paths: list[str] = []
    rg_executable = shutil.which("rg")
    if rg_executable is None:
        result.degradation_reason = "rg_unavailable"

    def add_hit(rel: str, line_no: int, text: str, file_hash: str | None = None) -> None:
        rows[(rel, line_no)] = text[:200]
        result.hits.append(
            RetrievalHit(
                hit_id=f"{result.query_id}:{len(result.hits) + 1}",
                path=rel,
                range={
                    "start_line": line_no,
                    "start_column": None,
                    "end_line": line_no + 1,
                    "end_column": None,
                    "exact": False,
                },
                kind="text_match",
                summary=text[:200],
                source="text",
                content_hash=file_hash,
                excerpt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )

    for path in _candidate_files(
        context, root, glob=str(args.get("glob", "")), max_files=max_files, deadline=deadline
    ):
        if path is None:
            result.partial("candidate_files")
            break
        if files >= max_files:
            result.partial("candidate_files")
            break
        if bytes_read >= max_bytes:
            result.partial("search_read_bytes")
            break
        reason = _stop_reason(context, deadline)
        if reason:
            result.execution = "cancelled" if reason == "cancelled" else "timeout"
            result.partial(reason)
            break
        rel = _relative(context, path)
        files += 1
        scanned_paths.append(rel)
        if rg_executable and path.stat().st_size < min(
            limits.file_hash_bytes, max_bytes - bytes_read
        ):
            rg_rows, consumed, file_hash, rg_reason = _rg_small_file(
                path,
                executable=rg_executable,
                pattern=pattern,
                ignore_case=bool(args.get("ignore_case")),
                context_lines=context_lines,
                max_hits=max_hits - len(result.hits),
                deadline=deadline,
                context=context,
            )
            bytes_read += consumed
            if rg_reason in {"cancelled", "timeout", "file_changed"}:
                result.execution = rg_reason if rg_reason != "file_changed" else "error"
                result.partial(rg_reason)
                break
            if rg_reason == "binary":
                continue
            if rg_rows is not None:
                if file_hash:
                    result.dependency_versions[rel] = file_hash
                for line_no, text, is_match in rg_rows:
                    if is_match:
                        add_hit(rel, line_no, text, file_hash)
                    else:
                        rows[(rel, line_no)] = text
                if len(result.hits) >= max_hits:
                    result.partial("hits")
                    break
                continue
            result.degradation_reason = rg_reason or "rg_error"
        elif rg_executable:
            result.degradation_reason = "rg_input_exceeds_file_budget"
        state = ScanState()
        previous: deque[tuple[int, str]] = deque(maxlen=context_lines)
        after = 0
        try:
            for line_no, text, long_line in _lines(
                path,
                byte_limit=max_bytes - bytes_read,
                line_limit=limits.line_bytes,
                context=context,
                deadline=deadline,
                state=state,
            ):
                if regex.search(text):
                    for prev_no, prev_text in previous:
                        rows[(rel, prev_no)] = prev_text[:200]
                    after = context_lines
                    add_hit(rel, line_no, text)
                    if len(result.hits) >= max_hits:
                        result.partial("hits")
                        break
                elif after:
                    rows[(rel, line_no)] = text[:200]
                    after -= 1
                if long_line:
                    result.partial("long_line")
                previous.append((line_no, text))
        except OSError:
            result.partial("unreadable_file")
            continue
        bytes_read += state.bytes_read
        if state.eof and path.stat().st_size <= limits.file_hash_bytes and state.content_hash:
            result.dependency_versions[rel] = state.content_hash
            for hit in result.hits:
                if hit.path == rel:
                    hit.content_hash = state.content_hash
        if state.stop_reason:
            if state.stop_reason == "binary":
                result.hits = [hit for hit in result.hits if hit.path != rel]
                rows = {key: value for key, value in rows.items() if key[0] != rel}
                result.dependency_versions.pop(rel, None)
                continue
            result.partial(
                "search_read_bytes" if state.stop_reason == "scan_bytes" else state.stop_reason
            )
            if state.stop_reason == "cancelled":
                result.execution = "cancelled"
            elif state.stop_reason == "timeout":
                result.execution = "timeout"
            break
        if len(result.hits) >= max_hits:
            break
    result.budget_used.update(bytes_read=bytes_read, files_scanned=files)
    result.scanned_scope = {
        "root": _relative(context, root),
        "files_scanned": files,
        "paths": scanned_paths,
    }
    if not result.truncation_reasons:
        reason = _stop_reason(context, deadline)
        if reason:
            result.execution = "cancelled" if reason == "cancelled" else "timeout"
            result.partial(reason)
    if result.execution == "cancelled":
        return _finish(result, "Error: 查询已取消", started, limits)
    rendered = _render_matches([(p, n, text) for (p, n), text in sorted(rows.items())])
    return _finish(result, rendered, started, limits)
