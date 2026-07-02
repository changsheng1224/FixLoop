"""Orchestrator：纯 Python 编排器（不调 LLM）。

工作流：
1. _parse_issue() → 正则提取语言/异常类型/文件名 → RepairPlan
2. _match_skill() → 匹配 YAML Skill
3. _run_localizer() + _run_retriever() → 并行执行
4. _run_patcher() → 串行执行
"""

import re
import time
from pathlib import Path

import yaml

from src.state import (
    CandidatePatch,
    RepairPlan,
    RepairState,
    RetrievedContext,
    SuspectLocation,
    VerificationResult,
)


class Orchestrator:
    """纯 Python 修复编排器。

    不调 LLM，只做调度和状态管理。
    """

    def __init__(self, localizer, retriever, patcher, verifier=None):
        self.localizer = localizer
        self.retriever = retriever
        self.patcher = patcher
        self.verifier = verifier

    def repair(self, issue: str, max_retries: int = 3) -> RepairState:
        """执行修复流水线。

        Args:
            issue: Issue 描述（含堆栈和错误信息）。
            max_retries: 最大重试次数。

        Returns:
            RepairState 实例。
        """
        state = RepairState(issue_input=issue, max_retries=max_retries)
        timings = {}

        # Step 1: 解析 Issue → RepairPlan + 匹配 Skill
        t0 = time.time()
        state.repair_plan = self._parse_issue(issue)
        skill = self._match_skill(issue)
        if skill and state.repair_plan:
            state.repair_plan.estimated_impact = skill.get("suggested_tools", [])
        timings["parse_issue_ms"] = int((time.time() - t0) * 1000)

        # Step 2: 定位（并行：Localizer + Retriever）
        t0 = time.time()
        state.suspect_locations = self._run_localizer(state)
        timings["localizer_ms"] = int((time.time() - t0) * 1000)

        t0 = time.time()
        state.retrieved_context = self._run_retriever(state)
        timings["retriever_ms"] = int((time.time() - t0) * 1000)

        # Step 3: 修补 → 验证 → 自愈循环
        while state.retry_count < max_retries:
            t0 = time.time()
            state.candidate_patches = self._run_patcher(state)
            timings["patcher_ms"] = int((time.time() - t0) * 1000)

            if self.verifier is None:
                state.status = "patched"
                break

            # M6: 调用 Verifier
            t0 = time.time()
            state.verification_result = self._run_verifier(state)
            timings["verifier_ms"] = int((time.time() - t0) * 1000)

            if state.verification_result.all_passed:
                state.status = "fixed"
                break

            # 失败 → 构建反馈 → 重试
            state.feedback = self._build_feedback(state.verification_result)
            state.retry_count += 1

        state.node_timings = timings
        return state

    def _parse_issue(self, issue: str) -> RepairPlan:
        """正则解析 Issue 文本，提取语言/异常类型/文件名。"""
        plan = RepairPlan(language="python")

        # 异常类型
        exc_match = re.search(r"(\w+(?:Error|Exception|Warning))", issue)
        if exc_match:
            exc_type = exc_match.group(1)
            plan.issue_type = self._classify_error(exc_type)

        # 文件名和行号
        file_match = re.search(r'File\s+"([^"]+)"', issue)
        if not file_match:
            file_match = re.search(r"at (\S+\.py)", issue)
        if file_match:
            plan.suspect_files.append(file_match.group(1))

        # 行号
        line_match = re.search(r"line (\d+)", issue)
        if line_match and plan.suspect_files:
            plan.reasoning = f"{plan.suspect_files[0]}:{line_match.group(1)}"
        else:
            plan.reasoning = issue[:200]

        return plan

    def _match_skill(self, issue: str) -> dict | None:
        """从 YAML Skill 文件中匹配 Issue 对应的修复策略。"""
        skills_dir = Path(__file__).parent / "skills"
        if not skills_dir.exists():
            return None
        for yaml_file in skills_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                pattern = data.get("trigger_pattern", "")
                if pattern and re.search(pattern, issue):
                    return data
            except Exception:
                pass
        return None

    def _classify_error(self, exc_type: str) -> str:
        mapping = {
            "TypeError": "type_error",
            "ImportError": "import_error",
            "ModuleNotFoundError": "import_error",
            "AttributeError": "attribute_error",
            "ValueError": "value_error",
            "SyntaxError": "syntax_error",
        }
        return mapping.get(exc_type, "unknown")

    def _run_localizer(self, state: RepairState) -> list[SuspectLocation]:
        """调 Localizer Agent 定位嫌疑代码。"""
        plan = state.repair_plan
        prompt = self._localizer_prompt(plan)
        answer = self.localizer.ask(prompt)
        return self._parse_suspect_list(answer)

    def _run_retriever(self, state: RepairState) -> RetrievedContext:
        """调 Retriever Agent 搜索上下文。"""
        prompt = self._retriever_prompt(state.suspect_locations)
        answer = self.retriever.ask(prompt)
        return self._parse_retrieved_context(answer)

    def _run_patcher(self, state: RepairState) -> list[CandidatePatch]:
        """调 Patcher Agent 生成补丁。"""
        prompt = self._patcher_prompt(
            state.suspect_locations, state.retrieved_context, state.feedback
        )
        answer = self.patcher.ask(prompt)
        return self._parse_patches(answer)

    def _run_verifier(self, state: RepairState) -> "VerificationResult":
        """调 Verifier Agent 在容器内验证。"""
        prompt = self._verifier_prompt(state.candidate_patches, state.repair_plan)
        answer = self.verifier.ask(prompt)
        return self._parse_verification(answer)

    def _build_feedback(self, result: "VerificationResult") -> str:
        """构建失败反馈文本。"""
        lines = ["补丁验证失败。以下测试仍失败："]
        for log in result.failure_logs[:5]:
            lines.append(f"  - {log[:200]}")
        lines.append("请修改补丁解决这些问题。")
        return "\n".join(lines)

    # ---- Prompt 构建 ----

    def _localizer_prompt(self, plan: RepairPlan) -> str:
        parts = [f"定位以下问题：\n{plan.reasoning}"]
        if plan.suspect_files:
            parts.append(f"嫌疑文件: {', '.join(plan.suspect_files)}")
        parts.append("请用 ast_parse 和 stack_parse 定位，输出 SuspectList JSON。")
        return "\n".join(parts)

    def _retriever_prompt(self, suspects: list[SuspectLocation]) -> str:
        if not suspects:
            return "根据以下 Issue 搜索相关代码上下文。"
        parts = ["根据以下嫌疑位置搜索相关代码："]
        for s in suspects:
            parts.append(f"  - {s.file_path}:{s.start_line} {s.function_name or ''}")
        parts.append("请用 search/git_blame/find_test 搜索，输出 RetrievedContext JSON。")
        return "\n".join(parts)

    def _patcher_prompt(
        self,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
        feedback: str = "",
    ) -> str:
        parts = []
        if feedback:
            parts.append(f"[上一轮验证反馈]\n{feedback}\n")
        parts.append("基于以下信息生成修复补丁：")
        if suspects:
            parts.append("嫌疑位置:")
            for s in suspects:
                parts.append(f"  - {s.file_path}:{s.start_line} ({s.reason})")
        if context and context.related_tests:
            parts.append(f"相关测试: {', '.join(context.related_tests)}")
        parts.append(
            "请用 read_file 确认上下文后，用 patch_file 生成最小化 diff。"
            "输出 CandidatePatch JSON 列表。"
        )
        return "\n".join(parts)

    # ---- 解析 Agent 输出 ----

    def _parse_suspect_list(self, answer: str) -> list[SuspectLocation]:
        import json

        try:
            json_str = _extract_json_block(answer)
            data = json.loads(json_str)
            if isinstance(data, list):
                return [SuspectLocation.from_dict(item) for item in data]
        except (json.JSONDecodeError, KeyError):
            pass
        return []

    def _parse_retrieved_context(self, answer: str) -> RetrievedContext:
        import json

        try:
            json_str = _extract_json_block(answer)
            data = json.loads(json_str)
            if isinstance(data, dict):
                return RetrievedContext.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            pass
        return RetrievedContext()

    def _verifier_prompt(self, patches: list[CandidatePatch], plan: RepairPlan | None) -> str:
        parts = ["验证以下补丁："]
        for p in patches:
            parts.append(f"  - {p.file_path}: {p.explanation or p.diff[:80]}")
        repo = plan.suspect_files[0] if plan and plan.suspect_files else "."
        parts.append(f"请用 sandbox_build({repo!r}) 构建，再用 sandbox_test({repo!r}) 测试。")
        return "\n".join(parts)

    def _parse_verification(self, answer: str) -> "VerificationResult":
        import json

        try:
            json_str = _extract_json_block(answer)
            data = json.loads(json_str)
            if isinstance(data, dict):
                return VerificationResult.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            pass
        return VerificationResult()

    def _parse_patches(self, answer: str) -> list[CandidatePatch]:
        import json

        try:
            json_str = _extract_json_block(answer)
            data = json.loads(json_str)
            if isinstance(data, list):
                return [CandidatePatch.from_dict(item) for item in data]
        except (json.JSONDecodeError, KeyError):
            pass
        return []


def _extract_json_block(text: str) -> str:
    """从文本中提取 JSON 块（优先处理 markdown 代码块）。"""
    text = text.strip()

    # 1. 从 markdown ```json...``` 代码块提取
    m = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 2. 尝试直接作为 JSON 解析
    if text.startswith("[") or text.startswith("{"):
        return text

    # 3. 搜索最近邻的完整 JSON 块（从最后一个 [ 或 { 开始）
    for start_char in ("[", "{"):
        end_char = "]" if start_char == "[" else "}"
        last_start = text.rfind(start_char)
        if last_start >= 0:
            # 从该位置找到配对的闭合
            depth = 0
            for i in range(last_start, len(text)):
                if text[i] == start_char:
                    depth += 1
                elif text[i] == end_char:
                    depth -= 1
                    if depth == 0:
                        return text[last_start:i + 1]

    return text
