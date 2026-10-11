"""Fixed isolated workspace worker; project code is parsed and never imported.

Reads use only the mounted workspace. Writes produce conditional mutation plans;
the trusted filesystem broker must recheck every base before committing them.
Neither a plan nor a successful worker process is evidence of a completed write.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_runtime.code_exploration.io import (
    _candidate_files,
    grep_result,
    list_files_result,
    read_file_result,
)
from agent_runtime.code_exploration.models import RetrievalLimits
from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.workspace_contract import (
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    MAX_SOURCE_BYTES,
    WORKSPACE_WRITE_TOOLS,
    relative_path,
    tool_result_from_wire,
    tool_result_to_wire,
)
from agent_runtime.python_structure import python_structure
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult, ToolStatus
from agent_runtime.workspace import DOC_NAMES


@dataclass(frozen=True)
class WorkspaceMutation:
    path: str
    base_sha256: str | None
    content: str | None  # None means delete; empty string means an empty file.


@dataclass
class WorkspaceReply:
    result: ToolResult
    mutations: list[WorkspaceMutation] = field(default_factory=list)

    @classmethod
    def from_wire(cls, raw: dict, request: SandboxRequest) -> WorkspaceReply:
        """Treat worker output as untrusted; only the requested paths may commit."""
        request = SandboxRequest.from_wire(request.to_wire())
        if not isinstance(raw, dict) or set(raw) != {"tool_result", "mutations"}:
            raise ValueError("workspace_result_invalid: reply fields")
        if len(json.dumps(raw, ensure_ascii=True, allow_nan=False).encode()) > MAX_RESULT_BYTES:
            raise ValueError("workspace_result_invalid: reply limit")
        result = tool_result_from_wire(raw["tool_result"])
        items = raw["mutations"]
        if not isinstance(items, list) or len(items) > 128 or result.changed_files:
            raise ValueError("workspace_result_invalid: mutation receipt")
        if request.tool_name in WORKSPACE_WRITE_TOOLS:
            if request.tool_name == "workspace_restore":
                scope = {relative_path(path) for path in request.tool_args.get("files", {})}
            elif request.tool_name == "apply_patch":
                from agent_runtime.apply_patch_format import parse_apply_patch_text

                args = request.tool_args
                patch = args.get("patch") or args.get("diff") or args.get("input") or ""
                scope = {relative_path(op.path) for op in parse_apply_patch_text(patch)}
            else:
                scope = {relative_path(request.tool_args.get("path", ""))}
        else:
            scope = set()
        mutations, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"path", "base_sha256", "content"}:
                raise ValueError("workspace_result_invalid: mutation fields")
            path = relative_path(item["path"])
            content = item["content"]
            if (
                path != item["path"]
                or path not in scope
                or path not in request.base_versions
                or item["base_sha256"] != request.base_versions[path]
                or path.casefold() in seen
                or (content is not None and not isinstance(content, str))
                or (content is not None and len(content.encode("utf-8")) > MAX_SOURCE_BYTES)
            ):
                raise ValueError("workspace_result_invalid: mutation scope or base")
            seen.add(path.casefold())
            mutations.append(WorkspaceMutation(**item))
        if items and result.status != ToolStatus.DRY_RUN:
            raise ValueError("workspace_result_invalid: prepared mutation status")
        if result.status == ToolStatus.DRY_RUN and not items:
            raise ValueError("workspace_result_invalid: empty prepared mutation")
        if scope and result.status not in {
            ToolStatus.DRY_RUN,
            ToolStatus.NO_CHANGE,
            ToolStatus.ERROR,
            ToolStatus.REJECTED,
        }:
            raise ValueError("workspace_result_invalid: premature write success")
        for key in ("workspace_versions", "source_dependencies"):
            from .workspace_contract import validate_versions

            validate_versions(result.metadata.get(key, {}))
        return cls(result, mutations)

    def to_wire(self) -> dict:
        raw = {
            "tool_result": tool_result_to_wire(self.result),
            "mutations": [asdict(item) for item in self.mutations],
        }
        if len(json.dumps(raw, ensure_ascii=True, allow_nan=False).encode()) > MAX_RESULT_BYTES:
            return {
                "tool_result": tool_result_to_wire(
                    ToolResult.error(
                        "Error: workspace reply too large",
                        code="output_too_large",
                        retryable=False,
                    )
                ),
                "mutations": [],
            }
        return raw


class WorkspaceError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _context(root: Path) -> ToolContext:
    root = root.resolve(strict=True)
    context = ToolContext(root=str(root))
    # Select the bounded pure-Python search implementation, never project rg.
    context.sandbox_backend = True
    default_resolve = context.path_resolver
    context.path_resolver = lambda raw: default_resolve(relative_path(raw, allow_root=True))
    return context


def _version(path: Path) -> tuple:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkspaceError("policy_denied", "regular single-link file required")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _source(context: ToolContext, raw: str, *, max_bytes=MAX_SOURCE_BYTES) -> tuple[bytes, str]:
    path = context.resolve(raw)
    before = _version(path)
    if before[2] > max_bytes:
        raise WorkspaceError("output_too_large", "source exceeds bounded analysis size")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise WorkspaceError("output_too_large", "source exceeds bounded analysis size")
    if before != _version(path) or len(data) != before[2]:
        raise WorkspaceError("stale_precondition", "file changed during read")
    return data, hashlib.sha256(data).hexdigest()


def _source_version(context: ToolContext, args: dict) -> ToolResult:
    raw = relative_path(args.get("path", ""))
    limit = args.get("max_bytes", MAX_SOURCE_BYTES)
    if type(limit) is not int or not 0 <= limit <= MAX_SOURCE_BYTES:
        raise WorkspaceError("invalid_arguments", "invalid source byte budget")
    data, digest = _source(context, raw, max_bytes=limit)
    return _evidence(
        ToolResult("source version checked", data={"bytes_read": len(data)}), raw, digest
    )


def _text(context: ToolContext, args: dict) -> ToolResult:
    raw = relative_path(args.get("path", ""))
    data, digest = _source(context, raw)
    return _evidence(ToolResult(data={"text": data.decode("utf-8")}), raw, digest)


def _evidence(result: ToolResult, path: str, digest: str) -> ToolResult:
    result.metadata.setdefault("workspace_versions", {})[path] = digest
    result.metadata.setdefault("source_dependencies", {})[path] = digest
    return result


def _read(context: ToolContext, args: dict) -> ToolResult:
    raw = relative_path(args.get("path", ""))
    path = context.resolve(raw)
    before = _version(path)
    result = read_file_result(context, args)
    if before != _version(path):
        raise WorkspaceError("stale_precondition", "file changed during range read")
    # A file that is too large to hash remains readable, but supplies no write base.
    if result.ok and before[2] <= MAX_SOURCE_BYTES:
        _, digest = _source(context, raw)
        if before != _version(path):
            raise WorkspaceError("stale_precondition", "file changed after range read")
        _evidence(result, raw, digest)
    result.metadata["read_version"] = {
        "path": raw,
        "version": list(before),
        "authority": "sandbox_workspace",
    }
    return result


def _ast(context: ToolContext, args: dict) -> ToolResult:
    raw = relative_path(args.get("path", ""))
    data, digest = _source(context, raw)
    symbols = python_structure(
        data.decode("utf-8"),
        start_line=int(args.get("start_line", 0) or 0),
        end_line=int(args.get("end_line", 0) or 0),
    )
    return _evidence(ToolResult(json.dumps(symbols, ensure_ascii=False, indent=2)), raw, digest)


def _inspect(context: ToolContext, args: dict) -> ToolResult:
    result = _read(context, args)
    if not result.ok:
        return result
    parsed = _ast(context, args)
    observed = result.metadata.get("workspace_versions", {})
    if observed != parsed.metadata.get("workspace_versions", {}):
        raise WorkspaceError("stale_precondition", "file changed between read and parse")
    result.content += "\n\n# AST\n" + parsed.content
    return result


def _find_test(context: ToolContext, args: dict) -> ToolResult:
    import re

    function = args.get("function_name", "")
    if not isinstance(function, str) or not function or len(function) > 256:
        raise WorkspaceError("invalid_arguments", "function_name required")
    module = Path(relative_path(args.get("file_path", ""))).stem
    pattern = re.compile(r"def (test_\w*" + re.escape(function) + r"\w*)")
    limits = RetrievalLimits()
    deadline = time.monotonic() + limits.timeout_s
    matches, versions = [], {}
    total_bytes, scanned, partial = 0, 0, False
    for path in _candidate_files(
        context,
        Path(context.root),
        glob="test_*.py",
        max_files=limits.search_files,
        deadline=deadline,
    ):
        if path is None or time.monotonic() >= deadline:
            partial = True
            break
        rel = path.relative_to(context.root).as_posix()
        if not {"test", "tests"}.intersection(Path(rel).parts[:-1]):
            continue
        scanned += 1
        size = _version(path)[2]
        if total_bytes + size > limits.search_read_bytes:
            partial = True
            break
        data, digest = _source(context, rel)
        total_bytes += len(data)
        versions[rel] = digest
        if path.name == f"test_{module}.py":
            matches.append({"test_file": rel, "confidence": 0.9, "strategy": "filename_match"})
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for hit in pattern.finditer(text):
            matches.append(
                {
                    "test_file": rel,
                    "test_function": hit.group(1),
                    "confidence": 0.7,
                    "strategy": "function_name_match",
                }
            )
            if len(matches) >= limits.max_hits:
                partial = True
                break
        if partial:
            break
    if time.monotonic() >= deadline:
        partial = True
    matches.sort(key=lambda item: item["confidence"], reverse=True)
    return ToolResult(
        json.dumps(matches, ensure_ascii=False, indent=2) if matches else "(未找到对应测试)",
        metadata={
            "source_dependencies": versions,
            "search_complete": not partial,
            "files_scanned": scanned,
            "bytes_read": total_bytes,
        },
        status=ToolStatus.PARTIAL if partial else ToolStatus.SUCCESS,
        error_code="partial_result" if partial else "",
        retryable=partial,
    )


def _metadata(context: ToolContext, args: dict) -> ToolResult:
    if args:
        raise WorkspaceError("invalid_arguments", "metadata accepts no model paths")
    docs, versions = {}, {}
    for name in DOC_NAMES:
        try:
            data, digest = _source(context, name)
        except FileNotFoundError:
            continue
        # The existing prompt document limit is intentionally retained.
        docs[name] = data.decode("utf-8")[:4000]
        versions[name] = digest
    return ToolResult(
        "workspace metadata collected",
        data={"doc_contents": docs, "git_available": False},
        metadata={"source_dependencies": versions, "workspace_versions": versions},
    )


def _checked_patch(source: str, args: dict) -> str:
    import re

    from agent_runtime.patch_engine import apply_plan, parse_patch_input

    plan = parse_patch_input(args)
    if plan.mode == "legacy":
        updated = apply_plan(source, plan)
        if updated is None:
            raise WorkspaceError("stale_preimage", "replacement preimage must match exactly once")
        return updated
    # Apply against original coordinates; earlier insertions must not shift a later hunk.
    original = source.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in source else "\n"
    output, cursor = [], 0
    for hunk in plan.hunks:
        header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", hunk.header)
        if header is None:
            raise WorkspaceError("invalid_arguments", "invalid hunk header")
        start = int(header[1]) - 1 if int(header[2] or 1) else int(header[1])
        old_count, new_count = int(header[2] or 1), int(header[4] or 1)
        if start < cursor or start > len(original):
            raise WorkspaceError("stale_preimage", "overlapping or out of range hunk")
        output.extend(original[cursor:start])
        cursor, removed, added = start, 0, 0
        for kind, text in hunk.lines:
            if kind in (" ", "-"):
                if cursor >= len(original) or original[cursor].rstrip("\r\n") != text:
                    raise WorkspaceError("stale_preimage", "hunk preimage mismatch")
                if kind == " ":
                    output.append(original[cursor])
                cursor += 1
                removed += 1
            if kind == "+":
                output.append(text + newline)
            if kind in (" ", "+"):
                added += 1
        if (removed, added) != (old_count, new_count):
            raise WorkspaceError("invalid_arguments", "hunk line counts mismatch")
    output.extend(original[cursor:])
    return "".join(output)


def _prepare_writes(context: ToolContext, request: SandboxRequest) -> WorkspaceReply:
    from agent_runtime.apply_patch_format import parse_apply_patch_text
    from agent_runtime.patch_engine import normalize_hunk_headers

    args = request.tool_args
    if request.tool_name == "apply_patch":
        patch = args.get("patch") or args.get("diff") or args.get("input") or ""
        if not isinstance(patch, str):
            raise WorkspaceError("invalid_arguments", "patch text required")
        operations = [(op.path, op.action, op.diff) for op in parse_apply_patch_text(patch)]
    else:
        operations = [(args.get("path", ""), request.tool_name, "")]
    for key in ("base_hash", "base_sha256"):
        if key in args and (
            len(operations) != 1
            or args[key] != request.base_versions.get(relative_path(operations[0][0]))
        ):
            raise WorkspaceError("stale_precondition", "explicit base differs from envelope")
    mutations, seen = [], set()
    # Validate every file, version, preimage and Python syntax before returning any plan.
    for raw, action, diff in operations:
        raw = relative_path(raw)
        if raw.casefold() in seen:
            raise WorkspaceError("invalid_arguments", "duplicate mutation path")
        seen.add(raw.casefold())
        if raw not in request.base_versions:
            raise WorkspaceError("stale_precondition", f"base version required: {raw}")
        expected = request.base_versions[raw]
        try:
            data, digest = _source(context, raw)
        except FileNotFoundError:
            data, digest = b"", None
        if digest != expected:
            raise WorkspaceError("stale_preimage", f"base version changed: {raw}")
        source = data.decode("utf-8")
        if action == "delete":
            if digest is None:
                raise WorkspaceError("stale_preimage", "delete target missing")
            updated = None
        elif action == "add":
            if digest is not None:
                raise WorkspaceError("stale_preimage", "add target exists")
            updated = diff if diff.endswith("\n") else diff + "\n"
        elif action == "write_file":
            content = args.get("content", "")
            if not isinstance(content, str) or type(args.get("append", False)) is not bool:
                raise WorkspaceError("invalid_arguments", "invalid write content or append")
            updated = source + content if args.get("append", False) else content
        else:
            if digest is None:
                raise WorkspaceError("stale_preimage", "patch target missing")
            patch_args = (
                {"diff": normalize_hunk_headers(diff, source)} if action == "update" else args
            )
            updated = _checked_patch(source, patch_args)
        if updated is not None:
            if len(updated.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise WorkspaceError("output_too_large", "edited source exceeds limit")
            if raw.endswith(".py"):
                try:
                    compile(updated, raw, "exec", dont_inherit=True)
                except SyntaxError as exc:
                    raise WorkspaceError(
                        "edit_lint_reject", f"Python syntax error at line {exc.lineno}"
                    ) from exc
        if updated is None or updated != source or digest is None:
            mutations.append(WorkspaceMutation(raw, expected, updated))
    if set(request.base_versions) != {relative_path(raw) for raw, _, _ in operations}:
        raise WorkspaceError("invalid_arguments", "mutation version scope mismatch")
    return WorkspaceReply(
        ToolResult(
            "conditional workspace mutations prepared" if mutations else "workspace unchanged",
            status=ToolStatus.DRY_RUN if mutations else ToolStatus.NO_CHANGE,
            retryable=False,
        ),
        mutations,
    )


def _restore(context: ToolContext, request: SandboxRequest) -> WorkspaceReply:
    files = request.tool_args.get("files")
    if not isinstance(files, dict) or set(files) != set(request.base_versions):
        raise WorkspaceError("invalid_arguments", "restore scope differs from versions")
    mutations = []
    for raw, content in files.items():
        raw = relative_path(raw)
        if content is not None and not isinstance(content, str):
            raise WorkspaceError("invalid_arguments", "restore content must be text or null")
        try:
            data, digest = _source(context, raw)
        except FileNotFoundError:
            data, digest = b"", None
        if digest != request.base_versions[raw]:
            raise WorkspaceError("stale_precondition", "restore target changed")
        if content is None and digest is None:
            continue
        if content is None or content.encode() != data:
            mutations.append(WorkspaceMutation(raw, digest, content))
    return WorkspaceReply(ToolResult(status="dry_run" if mutations else "no_change"), mutations)


def _inventory(context: ToolContext, args: dict) -> ToolResult:
    from .workspace_inventory import content_version, inventory

    if args:
        raise WorkspaceError("invalid_arguments", "inventory accepts no paths")
    versions = inventory(Path(context.root))
    return ToolResult(
        "workspace inventoried",
        data=versions,
        metadata={"content_version": content_version(versions)},
    )


def _snapshot(context: ToolContext, args: dict) -> ToolResult:
    from .workspace_inventory import snapshot_page

    return ToolResult("snapshot page collected", data=snapshot_page(Path(context.root), args))


def execute_request(request: SandboxRequest, *, root: Path = Path("/workspace")) -> WorkspaceReply:
    started = time.monotonic()
    try:
        # Revalidate direct dataclass callers as well as stdin requests.
        request = SandboxRequest.from_wire(request.to_wire())
        if request.operation != "workspace":
            raise WorkspaceError("policy_denied", "workspace operation required")
        context = _context(root)
        if request.tool_name == "workspace_restore":
            reply = _restore(context, request)
        elif request.tool_name in WORKSPACE_WRITE_TOOLS:
            reply = _prepare_writes(context, request)
        else:
            handlers = {
                "read_file": _read,
                "list_files": list_files_result,
                "grep": grep_result,
                "ast_parse": _ast,
                "inspect_file": _inspect,
                "find_test": _find_test,
                "workspace_metadata": _metadata,
                "workspace_source_version": _source_version,
                "workspace_inventory": _inventory,
                "workspace_snapshot": _snapshot,
                "workspace_text": _text,
            }
            reply = WorkspaceReply(handlers[request.tool_name](context, request.tool_args))
    except WorkspaceError as exc:
        reply = WorkspaceReply(ToolResult.error(f"Error: {exc}", code=exc.code, retryable=False))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        code = (
            str(exc).split(":", 1)[0]
            if ":" in str(exc)
            else (
                "policy_denied"
                if "policy_denied" in str(exc) or "sensitive_path" in str(exc)
                else "invalid_arguments"
            )
        )
        reply = WorkspaceReply(ToolResult.error(f"Error: {exc}", code=code, retryable=False))
    reply.result.duration_ms = round((time.monotonic() - started) * 1000)
    return reply


def main() -> int:
    data = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    try:
        if len(data) > MAX_REQUEST_BYTES or not data.endswith(b"\n"):
            raise ValueError("request frame too large or incomplete")
        request = SandboxRequest.from_wire(json.loads(data))
        reply = execute_request(request)
    except (ValueError, TypeError, RecursionError) as exc:
        reply = WorkspaceReply(
            ToolResult.error(f"Error: {exc}", code="policy_denied", retryable=False)
        )
    print(json.dumps(reply.to_wire(), ensure_ascii=True, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
