"""Source-aware retrieval contracts shared by file, text, and later LSP tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from agent_runtime.tool_result import ToolErrorCode, ToolResult, ToolStatus


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RetrievalLimits:
    range_scan_bytes: int = 256 * 1024
    range_return_lines: int = 200
    range_return_bytes: int = 32 * 1024
    search_files: int = 300
    search_read_bytes: int = 2 * 1024 * 1024
    max_hits: int = 50
    visible_bytes: int = 64 * 1024
    file_hash_bytes: int = 256 * 1024
    line_bytes: int = 32 * 1024
    timeout_s: float = 3.0

    def lower(self, **overrides: int) -> RetrievalLimits:
        values = asdict(self)
        for name, value in overrides.items():
            if name not in values:
                raise ValueError(f"Unknown retrieval limit: {name}")
            values[name] = min(values[name], max(1, int(value)))
        return RetrievalLimits(**values)


@dataclass
class RetrievalHit:
    hit_id: str
    path: str
    range: dict | None
    kind: str
    summary: str
    source: str
    resolution: str = "syntactic"
    content_hash: str | None = None
    excerpt_hash: str | None = None
    server_id: str | None = None
    observed_at: str = field(default_factory=now_utc)


@dataclass
class RetrievalResult:
    query_type: str
    execution: str = "ok"
    completeness: str = "complete_in_scope"
    hits: list[RetrievalHit] = field(default_factory=list)
    scanned_scope: dict = field(default_factory=dict)
    truncation_reasons: list[str] = field(default_factory=list)
    degradation_reason: str | None = None
    dependency_versions: dict[str, str] = field(default_factory=dict)
    budget_used: dict = field(default_factory=dict)
    duration_ms: int = 0
    query_id: str = field(default_factory=lambda: uuid4().hex)
    observed_at: str = field(default_factory=now_utc)
    schema_version: str = "1"

    def partial(self, reason: str) -> None:
        self.completeness = "partial"
        if reason not in self.truncation_reasons:
            self.truncation_reasons.append(reason)

    def to_tool_result(self, content: str) -> ToolResult:
        metadata = {
            "retrieval_result": asdict(self),
            "source_dependencies": dict(self.dependency_versions),
        }
        status = ToolStatus.SUCCESS.value
        if self.execution in {"rejected", "cancelled"}:
            status = self.execution
        elif self.execution != "ok":
            status = ToolStatus.ERROR.value
        error_code = {
            "timeout": ToolErrorCode.TOOL_TIMEOUT.value,
            "cancelled": ToolErrorCode.TOOL_CANCELLED.value,
            "rejected": ToolErrorCode.INVALID_ARGUMENTS.value,
            "error": ToolErrorCode.TOOL_EXECUTION_FAILED.value,
        }.get(self.execution, "")
        return ToolResult(
            content=content,
            metadata=metadata,
            status=status,
            error_code=error_code,
            retryable=False,
        )
