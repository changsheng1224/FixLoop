"""评测结果数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CaseResult:
    """单个评测 Case 的运行结果。"""

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
    failure_tags: list[str] = field(default_factory=list)
    variant: str = ""
    run_index: int = 0
    total_tokens: int = 0
    token_usage: dict = field(default_factory=dict)
    permission_denied_by_tool: dict = field(default_factory=dict)
    expected_skill: str | None = None
    matched_skill: str | None = None
    skill_match: bool = False
    skill_labeled: bool = False

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        data = {
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
            "failure_tags": list(self.failure_tags),
            "variant": self.variant,
            "run_index": self.run_index,
            "total_tokens": self.total_tokens,
            "token_usage": self.token_usage,
        }
        if self.permission_denied_by_tool:
            data["permission_denied_by_tool"] = self.permission_denied_by_tool
        if self.expected_skill is not None or self.matched_skill is not None:
            data["expected_skill"] = self.expected_skill
            data["matched_skill"] = self.matched_skill
            data["skill_match"] = self.skill_match
        return data


@dataclass
class EvalReport:
    """整次评测的聚合报告。"""

    cases: list[CaseResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    by_type: dict = field(default_factory=dict)
    by_difficulty: dict = field(default_factory=dict)
    by_variant: dict = field(default_factory=dict)
    skill_metrics: dict = field(default_factory=dict)
    pass_at_k: dict = field(default_factory=dict)
    performance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict（含 summary 与 cases 列表）。"""
        data = {
            "summary": self.summary,
            "by_type": self.by_type,
            "by_difficulty": self.by_difficulty,
            "cases": [c.to_dict() for c in self.cases],
        }
        if self.by_variant:
            data["by_variant"] = self.by_variant
        if self.skill_metrics:
            data["skill_metrics"] = self.skill_metrics
        if self.pass_at_k:
            data["pass_at_k"] = self.pass_at_k
        if self.performance:
            data["performance"] = self.performance
        return data
