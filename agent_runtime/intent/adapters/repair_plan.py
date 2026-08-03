"""Adapt IntentResult (repair channel) → RepairPlan without changing prompt_router."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_runtime.intent.graph import merge_constraints
from agent_runtime.intent.models import IntentResult
from src.repair.prompt_router import apply_prompt_routing
from src.state import RepairPlan

ClassifyFn = Callable[[str], tuple[str, str]]
ParseLineFn = Callable[[str, str], int]
DetectLangFn = Callable[..., tuple[str, str]]
LlmClassifyFn = Callable[[str], str | None]


class IssueIntentAdapter:
    """Fold repair-channel IntentResult into a single RepairPlan."""

    def __init__(
        self,
        *,
        classify_issue_type: ClassifyFn,
        parse_file_line: ParseLineFn,
        detect_language: DetectLangFn,
        llm_classify: LlmClassifyFn | None = None,
        repo_root: Path | str | None = None,
    ) -> None:
        self._classify = classify_issue_type
        self._parse_line = parse_file_line
        self._detect_language = detect_language
        self._llm_classify = llm_classify
        self._repo_root = Path(repo_root) if repo_root else None

    def to_repair_plan(self, result: IntentResult, issue: str) -> RepairPlan:
        graph = merge_constraints(result.graph)
        execs = [n for n in graph.nodes if n.role == "executable"]
        repair_nodes = [
            n for n in execs if n.primary in ("repair_issue", "repair_request")
        ]
        dropped: list[str] = []
        if len(repair_nodes) > 1:
            repair_nodes = sorted(repair_nodes, key=lambda n: n.confidence, reverse=True)
            dropped = [n.id for n in repair_nodes[1:]]
            repair_nodes = repair_nodes[:1]
        elif not repair_nodes and execs:
            # mis-routed: still try to parse original issue as repair
            repair_nodes = []

        node = repair_nodes[0] if repair_nodes else None
        text = (node.text if node and node.text.strip() else issue) or issue
        slots: dict[str, Any] = dict(result.slots)
        if node:
            for k, v in node.slots.items():
                if k not in slots or not slots[k]:
                    slots[k] = v
                elif isinstance(slots.get(k), list) and isinstance(v, list):
                    for item in v:
                        if item not in slots[k]:
                            slots[k].append(item)

        if dropped:
            result.raw_signals = dict(result.raw_signals or {})
            result.raw_signals["dropped_nodes"] = dropped

        plan = self._build_plan(text, slots)
        return plan

    def _build_plan(self, issue: str, slots: dict[str, Any]) -> RepairPlan:
        import re

        from agent_runtime.intent.stack_parse import extract_issue_slots, parse_stack

        plan = RepairPlan(language="python")
        issue_type, rule_name = self._classify(issue)
        plan.issue_type = issue_type

        has_import_err = bool(
            re.search(r"ModuleNotFoundError|ImportError", issue, re.IGNORECASE)
        )
        has_type_err = bool(re.search(r"TypeError", issue, re.IGNORECASE))
        if has_import_err and has_type_err:
            plan.issue_type = "composite"
            rule_name = "composite_dual"

        if rule_name in (
            "test_failure",
            "composite_keyword",
            "config_error",
            "logic_error",
            "composite_dual",
        ):
            plan.intent_parser = f"rule:{rule_name}"
        elif rule_name == "explicit_exception":
            plan.intent_parser = "rule"

        # Prefer stack-region files over noisy whole-text .py harvest
        stack_slots = extract_issue_slots(issue)
        for name in stack_slots.get("suspect_files") or []:
            name = str(name).replace("\\", "/")
            if name and name not in plan.suspect_files:
                plan.suspect_files.append(name)

        for file_match in re.finditer(r'File\s+"([^"]+)"', issue):
            name = file_match.group(1).replace("\\", "/")
            if name not in plan.suspect_files:
                plan.suspect_files.append(name)

        candidate_match = re.search(
            r"Candidate source files:\s*(.+)", issue, re.IGNORECASE
        )
        if candidate_match:
            for raw in candidate_match.group(1).split(","):
                name = raw.strip().replace("\\", "/")
                if name and name not in plan.suspect_files:
                    plan.suspect_files.append(name)

        if not plan.suspect_files:
            file_match = re.search(r"at (\S+\.py)", issue)
            if file_match:
                plan.suspect_files.append(Path(file_match.group(1)).name)

        # merge intent slots (already stack-aware from router)
        for name in slots.get("suspect_files") or []:
            name = str(name).replace("\\", "/")
            if name and name not in plan.suspect_files:
                plan.suspect_files.append(name)
        slot_type = slots.get("issue_type") or stack_slots.get("issue_type")
        if slot_type and (not plan.issue_type or plan.issue_type == "unknown"):
            plan.issue_type = str(slot_type)
        if slots.get("language"):
            plan.language = str(slots["language"])
            plan.language_source = "intent_slot"

        parsed = parse_stack(issue)
        line_no = None
        if parsed.top_frame:
            line_no = parsed.top_frame.line
        if line_no is None:
            line_no = self._parse_line(
                issue, plan.suspect_files[0] if plan.suspect_files else ""
            )
        if line_no and plan.suspect_files:
            plan.reasoning = f"{plan.suspect_files[0]}:{line_no}"
        else:
            plan.reasoning = issue[:200]

        if not plan.language_source:
            language, source = self._detect_language(
                issue,
                suspect_files=plan.suspect_files,
                repo_root=self._repo_root,
            )
            plan.language = language
            plan.language_source = source

        if not plan.issue_type or plan.issue_type == "unknown":
            if self._llm_classify is not None:
                llm_type = self._llm_classify(issue)
                if llm_type:
                    plan.issue_type = llm_type
                    plan.intent_parser = "llm"
                elif not plan.intent_parser:
                    plan.intent_parser = "rule"
            elif not plan.intent_parser:
                plan.intent_parser = "rule"
        elif not plan.intent_parser:
            plan.intent_parser = "rule"

        apply_prompt_routing(plan)
        return plan
