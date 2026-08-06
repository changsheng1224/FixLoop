"""issue_type → L2 prompt 变体与意图侧策略集中路由。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.prompts.loader import load_patcher_user_hint
from src.state import RepairPlan

_DEFAULT_PATCHER_VARIANT = "default"

EXCEPTION_TO_ISSUE_TYPE: dict[str, str] = {
    "TypeError": "type_error",
    "ImportError": "import_error",
    "ModuleNotFoundError": "import_error",
    "KeyError": "config_error",
    "AttributeError": "attribute_error",
    "ValueError": "value_error",
    "SyntaxError": "syntax_error",
}

_ISSUE_TYPE_ROUTES: dict[str, str] = {
    "type_error": "type_error",
    "import_error": "import_error",
    "attribute_error": "attribute_error",
    "logic_error": "logic_error",
    "config_error": "config_error",
    "composite": "composite",
    "test_failure": "default",
    "value_error": "type_error",
    "syntax_error": "default",
    "unknown": "default",
}

ROUTED_ISSUE_TYPES = frozenset(_ISSUE_TYPE_ROUTES)

# Skill 未命中时走 generic patcher（与 test_failure / unknown 路由一致）
GENERIC_FALLBACK_ISSUE_TYPES = frozenset({"unknown", "", "test_failure"})


def classify_exception(exc_type: str) -> str:
    """将异常类名映射为 ``issue_type``。"""
    return EXCEPTION_TO_ISSUE_TYPE.get(exc_type, "unknown")


@dataclass(frozen=True)
class PromptRouting:
    """一次 repair 解析出的 prompt 变体选择。"""

    source_issue_type: str
    patcher_variant: str

    @property
    def prompt_variants(self) -> dict[str, str]:
        return {"patcher": self.patcher_variant}

    def to_trace_payload(self) -> dict:
        return {
            "issue_type": self.source_issue_type,
            "prompt_variants": dict(self.prompt_variants),
        }


def resolve_prompt_routing(plan: RepairPlan | None) -> PromptRouting:
    """按 RepairPlan.issue_type 解析各 Agent 的 prompt 变体。"""
    issue_type = (plan.issue_type if plan else "").strip().lower()
    variant = _ISSUE_TYPE_ROUTES.get(issue_type, _DEFAULT_PATCHER_VARIANT)
    return PromptRouting(
        source_issue_type=plan.issue_type if plan else "",
        patcher_variant=variant,
    )


def apply_prompt_routing(plan: RepairPlan) -> PromptRouting:
    """解析路由并写入 ``plan.prompt_variants``。"""
    routing = resolve_prompt_routing(plan)
    plan.prompt_variants = dict(routing.prompt_variants)
    return routing


def patcher_variant_for(plan: RepairPlan | None) -> str:
    if not plan:
        return _DEFAULT_PATCHER_VARIANT
    return plan.prompt_variants.get("patcher", _DEFAULT_PATCHER_VARIANT)


def repair_plan_intent_snapshot(plan: RepairPlan) -> dict:
    """trace / report 用的意图快照（skill 解析完成后调用）。"""
    skill = plan.skill.to_dict()
    return {
        "issue_type": plan.issue_type,
        "language": plan.language,
        "language_source": plan.language_source,
        "prompt_variants": dict(plan.prompt_variants),
        "suspect_files": list(plan.suspect_files),
        "skill": skill,
        "matched_skill": skill["matched_skill"],
        "suggested_tools": skill["suggested_tools"],
        "skill_fallback_strategy": skill["fallback_strategy"],
        "skill_confidence": skill.get("confidence", 0.0),
        "intent_parser": plan.intent_parser or "rule",
    }


def fallback_suspect_uses_import_line(issue_type: str) -> bool:
    """Whether the rule seed should prioritize an import statement."""
    return issue_type.strip().lower() == "import_error"


def is_composite_multi_file(plan: RepairPlan | None) -> bool:
    return bool(plan and plan.issue_type == "composite" and plan.suspect_files)


def collect_patcher_user_hints(plan: RepairPlan | None, issue: str) -> list[str]:
    """Patcher user 模板中的 issue 侧启发式提示（外置文案）。"""
    hints: list[str] = []
    if plan and re.search(r"cannot import name", issue, re.IGNORECASE):
        hints.append(load_patcher_user_hint("cannot_import_name"))
    if is_composite_multi_file(plan):
        assert plan is not None
        hints.append(
            load_patcher_user_hint(
                "composite",
                file_count=len(plan.suspect_files),
            )
        )
    if issue and "concatenate str" in issue.lower():
        hints.append(load_patcher_user_hint("concatenate_str"))
    return hints
