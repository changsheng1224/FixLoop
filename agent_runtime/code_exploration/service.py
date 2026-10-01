"""Task-local semantic lookup with explicit bounded candidate fallback."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from agent_runtime.code_exploration.io import _limits, grep_result
from agent_runtime.code_exploration.lsp import (
    LspClient,
    LspError,
    from_lsp_character,
    path_to_uri,
    to_lsp_character,
    uri_to_path,
)
from agent_runtime.code_exploration.models import RetrievalHit, RetrievalResult
from agent_runtime.code_exploration.relations import (
    ObservedEvidence,
    build_view,
    new_epoch,
    parse_python,
)
from agent_runtime.sensitive_paths import is_sensitive_path
from agent_runtime.tool_result import ToolResult


def _snapshot(path: Path, limit: int) -> tuple[str, str]:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file exceeds LSP content budget")
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def _identifier(text: str, line: int, column: int) -> str:
    lines = text.splitlines()
    if line < 1 or line > len(lines) or column < 1 or column > len(lines[line - 1]):
        return ""
    row = lines[line - 1]
    for match in re.finditer(r"[^\W\d]\w*", row):
        if match.start() <= column - 1 < match.end():
            return match.group()
    return ""


class CodeExplorationService:
    def __init__(self, context, *, mode: str = "text", server_argv: tuple[str, ...] | None = None):
        self.context = context
        self.mode = mode
        # Executable comes from the host process environment, never tool arguments or repo files.
        found = shutil.which("pylsp")
        self.server_argv = server_argv if server_argv is not None else ((found,) if found else ())
        self.client: LspClient | None = None
        self.unavailable_reason: str | None = None
        self.epoch = str((context.observation_state or {}).get("exploration_epoch") or new_epoch())
        self.view_revision = 0
        self.evidence: dict[str, ObservedEvidence] = {}
        self.parsed_cache: dict[str, tuple[str, object]] = {}
        self.snippet_cache: dict[tuple[str, str, int, int], str] = {}
        self.pending_candidates: list[dict] = []
        self.omitted_evidence = 0
        self.last_invalidation_reason = ""
        self.scope = self._scope()

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def _scope(self) -> tuple[str, str, str, str]:
        state = self.context.observation_state or {}
        identity = state.get("session_identity") or {}
        return (
            str(identity.get("workspace_id", "")),
            str(identity.get("session_id", "")),
            str(identity.get("task_id", "")),
            str(identity.get("run_id", "")),
        )

    def emit(self, kind: str, payload: dict | None = None) -> None:
        sink = getattr(self.context, "exploration_event_sink", None)
        if sink is None:
            return
        _, _, task_id, run_id = self._scope()
        try:
            sink(
                kind, {"task_id": task_id, "run_id": run_id, "epoch": self.epoch, **(payload or {})}
            )
        except Exception:
            pass

    def invalidate(self, reason: str = "source_changed") -> None:
        self.last_invalidation_reason = reason
        self.epoch = new_epoch()
        self.view_revision += 1
        self.evidence.clear()
        self.parsed_cache.clear()
        self.snippet_cache.clear()
        self.pending_candidates.clear()
        self.omitted_evidence = 0
        state = self.context.observation_state
        if state is not None:
            state["exploration_epoch"] = self.epoch
        self.emit("exploration_invalidated", {"reason": reason})

    def observe(self, tool: str, args: dict, retrieval: dict, observation_id: str) -> None:
        """Bind a retrieval to its actual Observation ID after storage."""
        if self.mode != "relations" or tool not in {
            "read_file",
            "grep",
            "search",
            "code_lookup",
            "ast_parse",
            "inspect_file",
        }:
            return
        if self.scope != self._scope():
            self.invalidate("task_changed")
            self.scope = self._scope()
        all_versions = dict(retrieval.get("dependency_versions") or {})
        hits = tuple(retrieval.get("hits") or ())
        if not all_versions or not hits or retrieval.get("execution") != "ok":
            return
        hit_paths = list(
            dict.fromkeys(
                str(hit.get("path", "")) for hit in hits if hit.get("path") in all_versions
            )
        )
        selected_paths = (hit_paths or list(all_versions))[:8]
        versions = {path: all_versions[path] for path in selected_paths}
        self.omitted_evidence += max(0, len(all_versions) - len(versions))
        self.pending_candidates.clear()
        self.evidence[observation_id] = ObservedEvidence(
            observation_id,
            tool,
            dict(args),
            hits,
            versions,
            str(retrieval.get("observed_at", "")),
            dict(retrieval),
        )
        while (
            len(self.evidence) > 8
            or len({path for item in self.evidence.values() for path in item.versions}) > 8
        ):
            self.evidence.pop(next(iter(self.evidence)))
            self.omitted_evidence += 1

    def validate_view(self, source_checks=None) -> bool:
        """Conservatively discard the whole view on any source or record change."""
        if self.scope != self._scope():
            self.invalidate("task_changed")
            self.scope = self._scope()
            return False
        if not self.evidence:
            return True
        from agent_runtime.code_exploration.consumption import SourceChecks
        from agent_runtime.context_runtime import ObservationStore

        checks = source_checks or SourceChecks(self.context)
        store = ObservationStore(
            self.context.observation_state or {}, self.context.root, self.context.state_root
        )
        try:
            for item in self.evidence.values():
                record = store.get(item.observation_id)
                if record is None or record.stale or record.lifecycle != "active":
                    self.invalidate("observation_stale")
                    return False
                if not store.expand(item.observation_id):
                    self.invalidate("observation_checksum")
                    return False
                freshness, reason = checks.check(item.versions, Path(self.context.root).resolve())
                if freshness != "fresh":
                    self.invalidate(reason)
                    return False
            return True
        finally:
            store.close()

    def relations(self, args: dict) -> ToolResult:
        if self.mode != "relations":
            return ToolResult(
                content="code_relations disabled outside relations mode",
                metadata={"relation_view": {"status": "disabled"}},
                status="rejected",
            )
        if self._cancelled():
            return ToolResult(content="code_relations cancelled", status="cancelled")
        if not self.validate_view():
            return ToolResult(
                content="Previously observed source changed; relation view reset.",
                metadata={"relation_view": {"epoch": self.epoch, "status": "invalidated"}},
            )
        max_files = min(8, max(1, int(args.get("max_files", 8))))
        top_k = min(6, max(1, int(args.get("top_k", 6))))
        token_budget = min(1500, max(1, int(args.get("token_budget", 1500))))
        parsed = {}
        for item in self.evidence.values():
            for relative, digest in item.versions.items():
                if not relative.endswith(".py") or relative in parsed:
                    continue
                if relative in self.parsed_cache and self.parsed_cache[relative][0] == digest:
                    parsed[relative] = self.parsed_cache[relative][1]
                    continue
                try:
                    content, actual = _snapshot(
                        self.context.resolve(relative), _limits(self.context).file_hash_bytes
                    )
                    if actual != digest:
                        self.invalidate("source_changed")
                        return ToolResult(content="Source changed; relation view reset.")
                    parsed[relative] = parse_python(relative, content)
                    self.parsed_cache[relative] = (digest, parsed[relative])
                except (OSError, ValueError, UnicodeError, SyntaxError):
                    continue
        self.view_revision += 1
        view = build_view(
            list(self.evidence.values()),
            parsed,
            epoch=self.epoch,
            revision=self.view_revision,
            max_files=max_files,
        )
        if self.omitted_evidence:
            view["truncation_reasons"].append("evidence_window")
            view["coverage"]["omitted_observations_or_paths"] = self.omitted_evidence
        candidates = []
        for node in view["nodes"]:
            if node["kind"] != "symbol":
                continue
            path = node["path"]
            refs = [item.observation_id for item in self.evidence.values() if path in item.versions]
            if not refs:
                continue
            candidates.append(
                {
                    "path": path,
                    "start_line": node["range"]["start_line"],
                    "end_line": min(node["range"]["end_line"], node["range"]["start_line"] + 20),
                    "content_hash": view["dependency_versions"][path],
                    "observation_id": refs[0],
                    "reason": "observed_definition",
                }
            )
            if len(candidates) >= top_k:
                break
        self.pending_candidates = candidates
        view["candidate_snippets"] = candidates
        view["token_budget"] = token_budget
        view["observed_at"] = max((item.observed_at for item in self.evidence.values()), default="")
        summary = {
            "epoch": view["epoch"],
            "view_revision": view["view_revision"],
            "covered_files": view["covered_files"],
            "inclusion_paths": view["inclusion_paths"],
            "coverage": view["coverage"],
            "truncation_reasons": view["truncation_reasons"],
            "edges": view["edges"],
            "candidate_snippets": candidates,
        }
        self.emit(
            "relation_view_built",
            {
                "view_revision": self.view_revision,
                "coverage": view["coverage"],
                "truncation_reasons": view["truncation_reasons"],
                "observation_refs": view["observation_refs"],
            },
        )
        retrieval = RetrievalResult(
            query_type="code_relations",
            completeness="partial",
            scanned_scope={"observed_files": view["covered_files"], "epoch": self.epoch},
            dependency_versions=view["dependency_versions"],
            truncation_reasons=["observed_evidence_only", *view["truncation_reasons"]],
            hits=[
                RetrievalHit(
                    hit_id=f"relation:{index}",
                    path=raw["path"],
                    range=None,
                    kind="symbol",
                    summary=raw["reason"],
                    source="python_ast",
                    resolution="syntactic",
                    content_hash=raw["content_hash"],
                )
                for index, raw in enumerate(candidates)
            ],
        )
        return ToolResult(
            content="Task-local relations (observed evidence only):\n"
            + json.dumps(summary, ensure_ascii=False),
            metadata={"relation_view": view, **retrieval.to_tool_result("").metadata},
        )

    def _cancelled(self) -> bool:
        token = getattr(self.context, "cancel_token", None)
        return bool(token and token.is_cancelled)

    def _candidate(
        self, symbol: str, reason: str, result: RetrievalResult, max_results: int
    ) -> ToolResult:
        result.degradation_reason = reason
        result.completeness = "unknown"
        if not symbol:
            result.partial("anchor_has_no_identifier")
            return result.to_tool_result(
                "No identifier at the requested position; no candidate search run."
            )
        if self._cancelled():
            result.execution = "cancelled"
            return result.to_tool_result("Code lookup cancelled.")
        search = grep_result(
            self.context,
            {
                "pattern": rf"\b{re.escape(symbol)}\b",
                "path": ".",
                "glob": "*.py",
                "max_results": max_results,
            },
        )
        source = search.metadata.get("retrieval_result", {})
        for raw in source.get("hits", []):
            result.hits.append(
                RetrievalHit(
                    hit_id=raw.get("hit_id", ""),
                    path=raw.get("path", ""),
                    range=raw.get("range"),
                    kind="candidate",
                    summary=raw.get("summary", ""),
                    source="text",
                    resolution="candidate",
                    content_hash=raw.get("content_hash"),
                )
            )
        result.dependency_versions.update(source.get("dependency_versions", {}))
        result.scanned_scope = source.get("scanned_scope", {})
        for reason_code in source.get("truncation_reasons", []):
            result.partial(reason_code)
        if source.get("execution") in {"cancelled", "rejected", "timeout", "error"}:
            result.execution = source["execution"]
            if result.execution in {"cancelled", "rejected"}:
                result.hits.clear()
        return result.to_tool_result(
            f"LSP unavailable ({reason}); text candidates for {symbol}:\n" + search.content
        )

    def lookup(self, args: dict) -> ToolResult:
        result = RetrievalResult(query_type="code_lookup", completeness="unknown")
        if self.mode not in {"lsp", "relations"}:
            result.execution = "rejected"
            result.degradation_reason = "disabled"
            return result.to_tool_result("code_lookup disabled in text mode")
        operation = args.get("operation", "definition")
        if operation not in {"definition", "references"}:
            result.execution = "rejected"
            return result.to_tool_result("Invalid code_lookup operation")
        try:
            line, column = int(args.get("line", 0)), int(args.get("column", 0))
            max_results = min(50, max(1, int(args.get("max_results", 50))))
            raw_path = str(args.get("path", ""))
            path = self.context.resolve(raw_path)
            if is_sensitive_path(raw_path) or is_sensitive_path(path) or path.suffix != ".py":
                raise ValueError("path is sensitive or not a Python file")
            text, digest = _snapshot(path, _limits(self.context).file_hash_bytes)
        except (OSError, ValueError, UnicodeError) as exc:
            result.execution = "rejected"
            return result.to_tool_result(f"Code lookup rejected: {exc}")
        if self._cancelled():
            result.execution = "cancelled"
            return result.to_tool_result("Code lookup cancelled.")
        symbol = _identifier(text, line, column)
        if not symbol:
            return self._candidate(symbol, "anchor_has_no_identifier", result, max_results)
        relative = path.relative_to(Path(self.context.root).resolve()).as_posix()
        for attempt in range(2):
            if self.unavailable_reason:
                return self._candidate(symbol, self.unavailable_reason, result, max_results)
            try:
                if self.client is None:
                    self.client = LspClient(self.server_argv, Path(self.context.root).resolve())
                    self.client.start()
                capability = (
                    "definitionProvider" if operation == "definition" else "referencesProvider"
                )
                if not self.client.capabilities.get(capability):
                    raise LspError("server capability unsupported")
                self.client.sync(path, text, digest)
                row = text.splitlines()[line - 1]
                params = {
                    "textDocument": {"uri": path_to_uri(path)},
                    "position": {
                        "line": line - 1,
                        "character": to_lsp_character(row, column - 1, self.client.encoding),
                    },
                }
                if operation == "references":
                    params["context"] = {"includeDeclaration": False}
                locations = self.client.request(
                    "textDocument/definition"
                    if operation == "definition"
                    else "textDocument/references",
                    params,
                    cancelled=self._cancelled,
                )
                if locations is None:
                    locations = []
                if isinstance(locations, dict):
                    locations = [locations]
                if not isinstance(locations, list):
                    raise LspError("invalid locations response")
                staged: list[RetrievalHit] = []
                versions = {relative: digest}
                stale = False
                for item in locations[:max_results]:
                    if not isinstance(item, dict):
                        raise LspError("invalid location")
                    uri = item.get("targetUri") or item.get("uri", "")
                    span = (
                        item.get("targetSelectionRange")
                        or item.get("targetRange")
                        or item.get("range")
                    )
                    try:
                        target = uri_to_path(uri)
                        safe = self.context.resolve(str(target))
                        if is_sensitive_path(safe):
                            raise ValueError("sensitive target")
                    except (LspError, ValueError):
                        staged.append(
                            RetrievalHit(
                                str(len(staged)),
                                "",
                                None,
                                operation,
                                "External or inaccessible location",
                                "lsp",
                                "external",
                            )
                        )
                        continue
                    try:
                        target_text, target_hash = _snapshot(
                            safe, _limits(self.context).file_hash_bytes
                        )
                        start = span["start"]
                        end = span["end"]
                        rows = target_text.splitlines()
                        sl, el = int(start["line"]), int(end["line"])
                        start_col = (
                            from_lsp_character(
                                rows[sl], int(start["character"]), self.client.encoding
                            )
                            + 1
                        )
                        end_col = (
                            from_lsp_character(
                                rows[el], int(end["character"]), self.client.encoding
                            )
                            + 1
                        )
                    except (
                        OSError,
                        UnicodeError,
                        ValueError,
                        IndexError,
                        KeyError,
                        TypeError,
                    ) as exc:
                        raise LspError("invalid target location") from exc
                    rel = safe.relative_to(Path(self.context.root).resolve()).as_posix()
                    versions[rel] = target_hash
                    staged.append(
                        RetrievalHit(
                            str(len(staged)),
                            rel,
                            {
                                "start": {"line": sl + 1, "column": start_col},
                                "end": {"line": el + 1, "column": end_col},
                                "exact": True,
                            },
                            operation,
                            symbol,
                            "lsp",
                            "resolved_by_lsp",
                            content_hash=target_hash,
                            server_id="pylsp",
                        )
                    )
                for rel, expected in versions.items():
                    try:
                        _, current = _snapshot(
                            self.context.resolve(rel), _limits(self.context).file_hash_bytes
                        )
                    except (OSError, ValueError, UnicodeError):
                        stale = True
                        break
                    if current != expected:
                        stale = True
                        break
                if stale:
                    if attempt == 0:
                        try:
                            text, digest = _snapshot(path, _limits(self.context).file_hash_bytes)
                        except (OSError, ValueError, UnicodeError):
                            result.partial("anchor_changed_during_lookup")
                            return result.to_tool_result("Anchor changed during lookup.")
                        symbol = _identifier(text, line, column)
                        if not symbol:
                            result.partial("anchor_changed_during_lookup")
                            return result.to_tool_result("Anchor changed during lookup.")
                        continue
                    result.partial("source_changed_during_lookup")
                    return result.to_tool_result(
                        "Source changed during lookup; discard LSP result."
                    )
                result.hits = staged
                result.dependency_versions = versions
                result.scanned_scope = {"anchor": relative, "server": "pylsp"}
                if len(locations) > max_results:
                    result.partial("max_hits")
                visible = [
                    f"{hit.path}:{hit.range['start']['line']}:{hit.range['start']['column']}"
                    if hit.range
                    else "external location"
                    for hit in staged
                ]
                return result.to_tool_result(
                    "LSP " + operation + " results:\n" + "\n".join(visible)
                )
            except LspError as exc:
                if self._cancelled() or str(exc) == "cancelled":
                    result.execution = "cancelled"
                    self.close()
                    return result.to_tool_result("Code lookup cancelled.")
                self.unavailable_reason = str(exc)
                self.close()
                return self._candidate(symbol, self.unavailable_reason, result, max_results)
        result.partial("source_changed_during_lookup")
        return result.to_tool_result("Source changed during lookup.")
