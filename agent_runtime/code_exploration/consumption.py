"""Bounded source checks and a shared model-facing retrieval contract."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from agent_runtime.code_exploration.io import _limits, _safe_path, _stop_reason


def retrieval_header(result: dict, freshness: str = "unknown") -> str:
    """Keep coverage distinct from freshness; never turn candidates into proof."""
    scope = json.dumps(result.get("scanned_scope", {}), ensure_ascii=False, sort_keys=True)
    provenance = sorted(
        {
            f"{hit.get('source', 'unknown')}:{hit.get('resolution', 'unknown')}"
            for hit in result.get("hits", [])
            if isinstance(hit, dict)
        }
    )
    paths = list(
        dict.fromkeys(
            hit.get("path", "") for hit in result.get("hits", []) if isinstance(hit, dict)
        )
    )[:8]
    return (
        f"[retrieval execution={result.get('execution', 'unknown')} "
        f"completeness={result.get('completeness', 'unknown')} freshness={freshness} "
        f"query_id={result.get('query_id', '')} observed_at={result.get('observed_at', '')}\n"
        f"scope={scope[:512]} reasons={','.join(result.get('truncation_reasons', []))[:160]} "
        f"degradation={str(result.get('degradation_reason') or 'none')[:160]} "
        f"provenance={','.join(provenance)[:160]}; "
        f"observed_candidates={json.dumps(paths, ensure_ascii=False)[:256]}; "
        "absence applies only to the scanned scope; candidates are not runtime-call proof]\n"
    )


class SourceChecks:
    """One request shares a byte/file/time budget and a version cache."""

    def __init__(self, context):
        self.context = context
        self.limits = _limits(context)
        self.deadline = time.monotonic() + self.limits.timeout_s
        self.bytes_read = 0
        self.checked_files = 0
        self.cache: dict[str, tuple[str, str]] = {}

    def check_retrieval(self, result: dict, workspace: Path | None) -> tuple[str, str]:
        versions = result.get("dependency_versions", {})
        status, reason = self.check(versions, workspace)
        if status != "fresh":
            return status, reason
        if any(
            not isinstance(hit, dict) or hit.get("path") not in versions
            for hit in result.get("hits", [])
        ):
            return "unknown", "unversioned_source"
        return status, reason

    def check(self, versions: dict, workspace: Path | None) -> tuple[str, str]:
        if workspace is None or Path(self.context.root).resolve() != workspace:
            return "unknown", "workspace_mismatch"
        if not isinstance(versions, dict) or not versions:
            return "unknown", "unversioned_source"
        for relative, expected in versions.items():
            if (
                not isinstance(relative, str)
                or not isinstance(expected, str)
                or len(expected) != 64
            ):
                return "unknown", "invalid_source_version"
            stopped = _stop_reason(self.context, self.deadline)
            if stopped:
                return "unknown", stopped
            path, error = _safe_path(self.context, relative)
            if path is not None:
                path = path.resolve()
            if error or path is None or not path.is_relative_to(workspace):
                return "stale", "source_policy_changed"
            key = str(path)
            if key not in self.cache:
                if self.checked_files >= self.limits.search_files:
                    return "unknown", "source_file_budget"
                self.checked_files += 1
                digest = hashlib.sha256()
                read = 0
                try:
                    with path.open("rb") as stream:
                        while True:
                            stopped = _stop_reason(self.context, self.deadline)
                            if stopped:
                                return "unknown", stopped
                            remaining = min(
                                self.limits.file_hash_bytes - read,
                                self.limits.search_read_bytes - self.bytes_read,
                            )
                            # stat only establishes EOF at a budget boundary; the digest
                            # always covers bytes actually read, never a metadata hash.
                            if remaining <= 0:
                                if stream.tell() == path.stat().st_size:
                                    break
                                return "unknown", "source_byte_budget"
                            data = stream.read(min(4096, remaining))
                            self.bytes_read += len(data)
                            read += len(data)
                            if not data:
                                break
                            digest.update(data)
                    self.cache[key] = ("fresh", digest.hexdigest())
                except OSError:
                    self.cache[key] = ("stale", "source_unavailable")
            status, actual = self.cache[key]
            if status != "fresh":
                return status, actual
            if actual != expected:
                return "stale", "source_changed"
        return "fresh", ""


def unavailable_evidence(observation_id: str, reason: str, freshness: str) -> str:
    return (
        f"[{observation_id}] code evidence unavailable: {reason}; freshness={freshness}. "
        "Read or query the source again with an authorized tool."
    )
