"""RepairState 数据模型：多 Agent 修复流水线的全部状态类型。

Agent 间通过结构化 dataclass 通信，不靠自然语言。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

__all__ = [
    "AgentAskRef",
    "CandidatePatch",
    "RepairPlan",
    "RepairState",
    "RetrievedContext",
    "SkillContext",
    "SuspectLocation",
    "VerificationResult",
]


@dataclass
class AgentAskRef:
    """单次 L2 phase 内 Agent 调用（ask 或 synthetic complete_once）。"""

    agent: str
    phase: str
    attempt: int
    task_id: str
    run_id: str
    started_ms: int = 0
    finished_ms: int = 0
    stop_reason: str = ""
    tool_steps: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AgentAskRef:
        return cls(
            agent=str(data.get("agent", "")),
            phase=str(data.get("phase", "")),
            attempt=int(data.get("attempt", 0) or 0),
            task_id=str(data.get("task_id", "")),
            run_id=str(data.get("run_id", "")),
            started_ms=int(data.get("started_ms", 0) or 0),
            finished_ms=int(data.get("finished_ms", 0) or 0),
            stop_reason=str(data.get("stop_reason", "")),
            tool_steps=int(data.get("tool_steps", 0) or 0),
        )


@dataclass
class SkillContext:
    """Skill 匹配结果与 fallback 策略（嵌套于 RepairPlan）。"""

    matched_skill: str | None = None
    suggested_tools: list[str] = field(default_factory=list)
    example_issue: str = ""
    guidance: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    example_patch: str = ""
    fallback_strategy: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "matched_skill": self.matched_skill,
            "suggested_tools": list(self.suggested_tools),
            "example_issue": self.example_issue,
            "guidance": list(self.guidance),
            "avoid": list(self.avoid),
            "example_patch": self.example_patch,
            "fallback_strategy": self.fallback_strategy,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> SkillContext:
        raw = data or {}
        return cls(
            matched_skill=raw.get("matched_skill"),
            suggested_tools=list(raw.get("suggested_tools") or []),
            example_issue=str(raw.get("example_issue") or raw.get("skill_example_issue") or ""),
            guidance=list(raw.get("guidance") or raw.get("skill_guidance") or []),
            avoid=list(raw.get("avoid") or raw.get("skill_avoid") or []),
            example_patch=str(raw.get("example_patch") or raw.get("skill_example_patch") or ""),
            fallback_strategy=str(
                raw.get("fallback_strategy") or raw.get("skill_fallback_strategy") or ""
            ),
            confidence=float(raw.get("confidence", 0.0)),
        )


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
    def from_dict(cls, data: dict) -> SuspectLocation:
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
class RepairSubTask:
    """可独立修复的子问题（V1.4-Bonus15b）。

    Attributes:
        id: 子任务标识（如 "fix_import"）。
        goal: 子任务目标描述。
        suspect_files: 该子任务的嫌疑文件列表。
        depends_on: 依赖的其他子任务 id 列表。
    """

    id: str
    goal: str
    suspect_files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "suspect_files": list(self.suspect_files),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: dict) -> RepairSubTask:
        return cls(
            id=data.get("id", ""),
            goal=data.get("goal", ""),
            suspect_files=list(data.get("suspect_files", [])),
            depends_on=list(data.get("depends_on", [])),
        )


@dataclass
class RepairPlan:
    """修复计划——由 Orchestrator 解析 Issue 后产出。

    Attributes:
        language: 目标语言（默认 python）。
        issue_type: 问题类型（"type_error" / "import_error" / "test_failure" 等）。
        suspect_files: 嫌疑文件列表。
        estimated_impact: 预估影响的文件列表。
        skill: Skill 匹配上下文（matched_skill · tools · guidance · fallback）。
        reasoning: 判定依据。
        prompt_variants: 各 Agent prompt 变体键（patcher / localizer）。
    """

    language: str = "python"
    language_source: str = ""
    issue_type: str = ""
    suspect_files: list[str] = field(default_factory=list)
    estimated_impact: list[str] = field(default_factory=list)
    skill: SkillContext = field(default_factory=SkillContext)
    reasoning: str = ""
    prompt_variants: dict[str, str] = field(default_factory=dict)
    intent_parser: str = ""
    subtasks: list[RepairSubTask] = field(default_factory=list)
    schema_version: str = "1.0"

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return {
            "language": self.language,
            "language_source": self.language_source,
            "issue_type": self.issue_type,
            "suspect_files": self.suspect_files,
            "estimated_impact": self.estimated_impact,
            "skill": self.skill.to_dict(),
            "reasoning": self.reasoning,
            "prompt_variants": dict(self.prompt_variants),
            "intent_parser": self.intent_parser,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RepairPlan:
        """从 dict 反序列化。"""
        skill = SkillContext.from_dict(data.get("skill"))
        if "skill" not in data:
            skill = SkillContext.from_dict(data)
        return cls(
            language=data.get("language", "python"),
            language_source=data.get("language_source", ""),
            issue_type=data.get("issue_type", ""),
            suspect_files=data.get("suspect_files", []),
            estimated_impact=data.get("estimated_impact", []),
            skill=skill,
            reasoning=data.get("reasoning", ""),
            prompt_variants=dict(data.get("prompt_variants") or {}),
            intent_parser=data.get("intent_parser", ""),
            subtasks=[RepairSubTask.from_dict(s) for s in data.get("subtasks", [])],
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
    def from_dict(cls, data: dict) -> RetrievedContext:
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
    def from_dict(cls, data: dict) -> CandidatePatch:
        """从 dict 反序列化。"""
        from src.repair.patch_applier import normalize_patch_text_field

        return cls(
            file_path=str(data.get("file_path", "") or ""),
            original_lines=normalize_patch_text_field(data.get("original_lines", "")),
            patched_lines=normalize_patch_text_field(data.get("patched_lines", "")),
            diff=str(data.get("diff", "") or ""),
            explanation=str(data.get("explanation", "") or ""),
            schema_version=str(data.get("schema_version", "1.0") or "1.0"),
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
    def from_dict(cls, data: dict) -> VerificationResult:
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


# 修复阶段枚举（与终态 status 分离）
REPAIR_PHASES = ("localize", "retrieve", "patch", "verify", "done", "failed")


@dataclass
class RepairState:
    """多 Agent 修复流水线的共享状态。

    Orchestrator 持有并驱动此状态在 Agent 间流转。
    phase 跟踪当前阶段，status 记录终态结果。
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
    phase: str = "localize"  # 当前阶段: localize|retrieve|patch|verify|done|failed
    status: str = "pending"  # 终态: pending|fixed|failed|exhausted|timeout|user_cancel
    failure_tags: list[str] = field(default_factory=list)
    node_timings: dict = field(default_factory=dict)
    agent_errors: dict = field(default_factory=dict)
    repair_run_id: str = ""
    agent_asks: list[AgentAskRef] = field(default_factory=list)
    blackboard_snapshot: dict = field(default_factory=dict)
    degraded_mode: bool = False
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
            "phase": self.phase,
            "status": self.status,
            "failure_tags": list(self.failure_tags),
            "node_timings": self.node_timings,
            "agent_errors": self.agent_errors,
            "repair_run_id": self.repair_run_id,
            "agent_asks": [ref.to_dict() for ref in self.agent_asks],
            "blackboard_snapshot": dict(self.blackboard_snapshot),
            "degraded_mode": self.degraded_mode,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RepairState:
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
            phase=data.get("phase", "localize"),
            status=data.get("status", "pending"),
            failure_tags=list(data.get("failure_tags", [])),
            node_timings=data.get("node_timings", {}),
            agent_errors=data.get("agent_errors", {}),
            repair_run_id=data.get("repair_run_id", ""),
            agent_asks=[AgentAskRef.from_dict(item) for item in data.get("agent_asks", [])],
            blackboard_snapshot=dict(data.get("blackboard_snapshot") or {}),
            degraded_mode=data.get("degraded_mode", False),
            schema_version=data.get("schema_version", "1.0"),
        )
