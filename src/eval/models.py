"""评测结果数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CaseResult:
    case_id: str
    issue_type: str = ""
    difficulty: str = ""
    fixed: bool = False
    retry_count: int = 0
    actual_patch: str = ""
    actual_lines: int = 0
    minimal_lines: int = 0
    duration_ms: int = 0
    agent_timings: dict = field(default_factory=dict)
    error: str = ""
    introduced_regression: bool = False
    status: str = ""
    variant: str = ""
    run_index: int = 0
    total_tokens: int = 0
    token_usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "issue_type": self.issue_type,
            "difficulty": self.difficulty,
            "fixed": self.fixed,
            "retry_count": self.retry_count,
            "actual_patch": self.actual_patch,
            "actual_lines": self.actual_lines,
            "minimal_lines": self.minimal_lines,
            "duration_ms": self.duration_ms,
            "agent_timings": self.agent_timings,
            "error": self.error,
            "introduced_regression": self.introduced_regression,
            "status": self.status,
            "variant": self.variant,
            "run_index": self.run_index,
            "total_tokens": self.total_tokens,
            "token_usage": self.token_usage,
        }


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    by_type: dict = field(default_factory=dict)
    by_difficulty: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "by_type": self.by_type,
            "by_difficulty": self.by_difficulty,
            "cases": [c.to_dict() for c in self.cases],
        }
