"""Issue preparation with explicit source, model and routing dependencies."""

import re
from collections import namedtuple

from src.repair.language_detect import detect_repair_language
from src.repair.prompt_router import (
    ROUTED_ISSUE_TYPES,
    classify_exception,
    fallback_suspect_uses_import_line,
)
from src.state import RepairPlan, SuspectLocation

# issue_type 分类规则链（按序匹配，首次命中即返回）
_IssueTypeRule = namedtuple("_IssueTypeRule", ["name", "pattern", "issue_type"])


_ISSUE_TYPE_RULES: list[_IssueTypeRule] = [
    # test_failure 必须在 explicit_exception 之前：
    # "FAILED test_app.py::test_add - AssertionError" 应识别为 test_failure 而非 type_error
    _IssueTypeRule(
        "test_failure",
        r"(?i)(FAILED\s+\S*test|AssertionError|assert\s+\S+\s*[=!<>])",
        "test_failure",
    ),
    _IssueTypeRule("explicit_exception", r"\w+(?:Error|Exception|Warning)", None),
    _IssueTypeRule("composite_keyword", r"(?i)composite", "composite"),
    _IssueTypeRule("config_error", r"(?i)pyproject\.toml|\[tool\.", "config_error"),
    _IssueTypeRule(
        "logic_error",
        r"(?i)(wrong\s+(result|output|return|value)|"
        r"incorrect\s+(result|output|behavio)|"
        r"should\s+(return|be|have)|"
        r"expected\s+\S+\s+but|"
        r"unexpected\s+(result|behavio)|"
        r"\bbug\b.*\b(function|logic|code)\b)",
        "logic_error",
    ),
]


class IssuePreparationService:
    def __init__(
        self,
        repo_root,
        *,
        read_source,
        parse_line=None,
        classify=None,
        patcher=None,
        light_client=None,
    ):
        self.repo_root = repo_root
        self.read_source = read_source
        self.parse_line = parse_line or self.parse_file_line
        self.patcher = patcher
        self.light_client = light_client
        self.classify = classify or self.classify_issue_type

    def prepare(self, issue, route_context):
        from agent_runtime.intent.router import IntentRouter
        from src.repair.intent_adapter import IssueIntentAdapter

        result = IntentRouter().route(issue, route_context)
        adapter = IssueIntentAdapter(
            classify_issue_type=self.classify,
            parse_file_line=self.parse_line,
            detect_language=detect_repair_language,
            llm_classify=self.classify_with_model,
            repo_root=self.repo_root,
        )
        return adapter.to_repair_plan(result, issue), result

    @staticmethod
    def classify_issue_type(issue: str) -> tuple[str, str]:
        """规则链分类：依次匹配 _ISSUE_TYPE_RULES，首次命中返回 (issue_type, rule_name)。

        explicit_exception 命中时调用 classify_exception 归一化异常类名。
        """
        for rule in _ISSUE_TYPE_RULES:
            m = re.search(rule.pattern, issue)
            if not m:
                continue
            if rule.name == "explicit_exception":
                return classify_exception(m.group(0)), rule.name
            return rule.issue_type, rule.name
        return "unknown", "none"

    def classify_with_model(self, issue: str) -> str | None:
        """用 light_client 将歧义 issue 分类为已知 issue_type。"""
        light = self.light_client
        if light is None:
            return None
        types = ", ".join(ROUTED_ISSUE_TYPES)
        prompt = (
            f"Classify this software issue into exactly one category: {types}.\n"
            f"Reply with ONLY the category name.\n\n{issue[:800]}"
        )
        try:
            from agent_runtime.model_call_budget import complete_text

            raw = complete_text(self.patcher, light, prompt, max_output_tokens=32, purpose="repair")
            for word in raw.strip().split():
                word = word.strip(".,;:\"'").lower()
                if word in ROUTED_ISSUE_TYPES:
                    return word
        except Exception:
            pass
        return None

    @staticmethod
    def parse_file_line(issue: str, file_path: str) -> int:
        """从 issue 文本提取行号（支持 file.py:42 或 line 42）。"""
        if file_path:
            m = re.search(rf"{re.escape(file_path)}:(\d+)", issue)
            if m:
                return int(m.group(1))
        m = re.search(r"line (\d+)", issue, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return 0

    def fallback_suspects_from_plan(
        self,
        plan: RepairPlan,
        issue: str,
    ) -> list[SuspectLocation]:
        """Localizer 无输出时，从 RepairPlan 生成粗粒度嫌疑位置。"""
        if not plan.suspect_files:
            return []
        suspects: list[SuspectLocation] = []
        for file_path in plan.suspect_files:
            line = self.parse_line(issue, file_path) or 1
            reason = "RepairPlan 降级定位"
            if fallback_suspect_uses_import_line(plan.issue_type):
                import_line = self.find_import_line_number(file_path)
                if import_line:
                    line = import_line
                reason = "import 语句"
            suspects.append(
                SuspectLocation(
                    file_path=file_path,
                    start_line=line,
                    end_line=line,
                    reason=reason,
                    confidence=0.7,
                )
            )
        return suspects

    def find_import_line_number(self, file_path: str) -> int | None:
        """定位文件中第一条 import/from 语句行号。"""
        for i, line in enumerate(self.read_source(file_path).splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("from ", "import ")):
                return i
        return None
