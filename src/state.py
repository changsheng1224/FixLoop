"""RepairState 数据模型：多 Agent 修复流水线的全部状态类型。

Agent 间通过结构化 dataclass 通信，不靠自然语言。
"""

from dataclasses import dataclass, field


@dataclass
class SuspectLocation:
    """代码定位结果——由 Localizer 产出。

    Attributes:
        file_path: 文件路径。
        start_line: 嫌疑代码起始行。
        end_line: 嫌疑代码结束行。
        function_name: 所在函数/方法名（可选）。
        class_name: 所在类名（可选）。
        reason: 定位依据（"堆栈指向" / "AST 分析" / "git blame"）。
        confidence: 置信度 0.0~1.0。
    """

    file_path: str
    start_line: int
    end_line: int
    function_name: str | None = None
    class_name: str | None = None
    reason: str = ""
    confidence: float = 0.0
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return {
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "function_name": self.function_name,
            "class_name": self.class_name,
            "reason": self.reason,
            "confidence": self.confidence,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SuspectLocation":
        """从 dict 反序列化。"""
        return cls(
            file_path=data.get("file_path", ""),
            start_line=data.get("start_line", 0),
            end_line=data.get("end_line", 0),
            function_name=data.get("function_name"),
            class_name=data.get("class_name"),
            reason=data.get("reason", ""),
            confidence=data.get("confidence", 0.0),
            schema_version=data.get("schema_version", "1.0"),
        )


@dataclass
class RepairPlan:
    """修复计划——由 Orchestrator 解析 Issue 后产出。

    Attributes:
        language: 目标语言（默认 python）。
        issue_type: 问题类型（"type_error" / "import_error" / "test_failure" 等）。
        suspect_files: 嫌疑文件列表。
        estimated_impact: 预估影响的文件列表。
        reasoning: 判定依据。
    """

    language: str = "python"
    issue_type: str = ""
    suspect_files: list[str] = field(default_factory=list)
    estimated_impact: list[str] = field(default_factory=list)
    reasoning: str = ""
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return {
            "language": self.language,
            "issue_type": self.issue_type,
            "suspect_files": self.suspect_files,
            "estimated_impact": self.estimated_impact,
            "reasoning": self.reasoning,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RepairPlan":
        """从 dict 反序列化。"""
        return cls(
            language=data.get("language", "python"),
            issue_type=data.get("issue_type", ""),
            suspect_files=data.get("suspect_files", []),
            estimated_impact=data.get("estimated_impact", []),
            reasoning=data.get("reasoning", ""),
            schema_version=data.get("schema_version", "1.0"),
        )


@dataclass
class RetrievedContext:
    """检索上下文——由 Retriever 产出。

    Attributes:
        similar_snippets: 相似代码片段列表。
        caller_locations: 调用方位置列表。
        related_tests: 相关测试文件/用例列表。
        similar_fixes: 历史类似修复列表。
    """

    similar_snippets: list[dict] = field(default_factory=list)
    caller_locations: list[str] = field(default_factory=list)
    related_tests: list[str] = field(default_factory=list)
    similar_fixes: list[dict] = field(default_factory=list)
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return {
            "similar_snippets": self.similar_snippets,
            "caller_locations": self.caller_locations,
            "related_tests": self.related_tests,
            "similar_fixes": self.similar_fixes,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RetrievedContext":
        """从 dict 反序列化。"""
        return cls(
            similar_snippets=data.get("similar_snippets", []),
            caller_locations=data.get("caller_locations", []),
            related_tests=data.get("related_tests", []),
            similar_fixes=data.get("similar_fixes", []),
            schema_version=data.get("schema_version", "1.0"),
        )


@dataclass
class CandidatePatch:
    """候选补丁——由 Patcher 产出。

    Attributes:
        file_path: 目标文件。
        original_lines: 原始文本。
        patched_lines: 修补后文本。
        diff: unified diff 格式。
        explanation: 修改说明。
    """

    file_path: str = ""
    original_lines: str = ""
    patched_lines: str = ""
    diff: str = ""
    explanation: str = ""
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return {
            "file_path": self.file_path,
            "original_lines": self.original_lines,
            "patched_lines": self.patched_lines,
            "diff": self.diff,
            "explanation": self.explanation,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidatePatch":
        """从 dict 反序列化。"""
        return cls(
            file_path=data.get("file_path", ""),
            original_lines=data.get("original_lines", ""),
            patched_lines=data.get("patched_lines", ""),
            diff=data.get("diff", ""),
            explanation=data.get("explanation", ""),
            schema_version=data.get("schema_version", "1.0"),
        )


@dataclass
class VerificationResult:
    """验证结果——由 Verifier 产出。

    Attributes:
        all_passed: 所有测试是否通过。
        total_tests: 测试总数。
        passed: 通过数。
        failed: 失败数。
        error: 错误数。
        failure_logs: 失败日志列表。
        build_log: 构建日志。
        lint_issues: Lint 问题列表。
    """

    all_passed: bool = False
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    error: int = 0
    failure_logs: list[str] = field(default_factory=list)
    build_log: str = ""
    lint_issues: list[str] = field(default_factory=list)
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return {
            "all_passed": self.all_passed,
            "total_tests": self.total_tests,
            "passed": self.passed,
            "failed": self.failed,
            "error": self.error,
            "failure_logs": self.failure_logs,
            "build_log": self.build_log,
            "lint_issues": self.lint_issues,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VerificationResult":
        """从 dict 反序列化。"""
        return cls(
            all_passed=data.get("all_passed", False),
            total_tests=data.get("total_tests", 0),
            passed=data.get("passed", 0),
            failed=data.get("failed", 0),
            error=data.get("error", 0),
            failure_logs=data.get("failure_logs", []),
            build_log=data.get("build_log", ""),
            lint_issues=data.get("lint_issues", []),
            schema_version=data.get("schema_version", "1.0"),
        )


@dataclass
class RepairState:
    """多 Agent 修复流水线的共享状态。

    Orchestrator 持有并驱动此状态在 Agent 间流转。
    """

    issue_input: str
    repair_plan: RepairPlan | None = None
    suspect_locations: list[SuspectLocation] = field(default_factory=list)
    retrieved_context: RetrievedContext | None = None
    candidate_patches: list[CandidatePatch] = field(default_factory=list)
    verification_result: VerificationResult | None = None
    feedback: str = ""
    retry_count: int = 0
    max_retries: int = 3
    status: str = "pending"  # pending / localizing / patched / fixed / failed / exhausted
    node_timings: dict = field(default_factory=dict)
    agent_errors: dict = field(default_factory=dict)
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return {
            "issue_input": self.issue_input,
            "repair_plan": self.repair_plan.to_dict() if self.repair_plan else None,
            "suspect_locations": [s.to_dict() for s in self.suspect_locations],
            "retrieved_context": (
                self.retrieved_context.to_dict() if self.retrieved_context else None
            ),
            "candidate_patches": [p.to_dict() for p in self.candidate_patches],
            "verification_result": (
                self.verification_result.to_dict() if self.verification_result else None
            ),
            "feedback": self.feedback,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "status": self.status,
            "node_timings": self.node_timings,
            "agent_errors": self.agent_errors,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RepairState":
        """从 dict 反序列化。"""
        return cls(
            issue_input=data.get("issue_input", ""),
            repair_plan=(
                RepairPlan.from_dict(data["repair_plan"]) if data.get("repair_plan") else None
            ),
            suspect_locations=[
                SuspectLocation.from_dict(s) for s in data.get("suspect_locations", [])
            ],
            retrieved_context=(
                RetrievedContext.from_dict(data["retrieved_context"])
                if data.get("retrieved_context")
                else None
            ),
            candidate_patches=[
                CandidatePatch.from_dict(p) for p in data.get("candidate_patches", [])
            ],
            verification_result=(
                VerificationResult.from_dict(data["verification_result"])
                if data.get("verification_result")
                else None
            ),
            feedback=data.get("feedback", ""),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            status=data.get("status", "pending"),
            node_timings=data.get("node_timings", {}),
            agent_errors=data.get("agent_errors", {}),
            schema_version=data.get("schema_version", "1.0"),
        )
