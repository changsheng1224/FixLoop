"""issue_type → L2 prompt 变体与意图侧策略集中路由。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.prompts.loader import load_patcher_user_hint
from src.state import RepairPlan

_DEFAULT_PATCHER_VARIANT = "default"
_DEFAULT_LOCALIZER_HINTS = "stack_first"

EXCEPTION_TO_ISSUE_TYPE: dict[str, str] = {
    "TypeError": "type_error",
    "ImportError": "import_error",
    "ModuleNotFoundError": "import_error",
    "KeyError": "config_error",
    "AttributeError": "attribute_error",
    "ValueError": "value_error",
    "SyntaxError": "syntax_error",
}

_ISSUE_TYPE_ROUTES: dict[str, dict[str, str]] = {
    "type_error": {"patcher": "type_error", "localizer": "stack_first"},
    "import_error": {"patcher": "import_error", "localizer": "import_first"},
    "attribute_error": {"patcher": "attribute_error", "localizer": "stack_first"},
    "logic_error": {"patcher": "logic_error", "localizer": "stack_first"},
    "config_error": {"patcher": "config_error", "localizer": "stack_first"},
    "composite": {"patcher": "composite", "localizer": "stack_first"},
    "test_failure": {"patcher": "default", "localizer": "stack_first"},
    "value_error": {"patcher": "type_error", "localizer": "stack_first"},
    "syntax_error": {"patcher": "default", "localizer": "stack_first"},
    "unknown": {"patcher": "default", "localizer": "stack_first"},
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
    localizer_hints_key: str

    @property
    def prompt_variants(self) -> dict[str, str]:
        return {
            "patcher": self.patcher_variant,
            "localizer": self.localizer_hints_key,
        }

    def to_trace_payload(self) -> dict:
        return {
            "issue_type": self.source_issue_type,
            "prompt_variants": dict(self.prompt_variants),
        }


def resolve_prompt_routing(plan: RepairPlan | None) -> PromptRouting:
    """按 RepairPlan.issue_type 解析各 Agent 的 prompt 变体。"""
    issue_type = (plan.issue_type if plan else "").strip().lower()
    route = _ISSUE_TYPE_ROUTES.get(
        issue_type,
        {
            "patcher": _DEFAULT_PATCHER_VARIANT,
            "localizer": _DEFAULT_LOCALIZER_HINTS,
        },
    )
    return PromptRouting(
        source_issue_type=plan.issue_type if plan else "",
        patcher_variant=route["patcher"],
        localizer_hints_key=route["localizer"],
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


def localizer_hints_key_for(plan: RepairPlan) -> str:
    return plan.prompt_variants.get("localizer", _DEFAULT_LOCALIZER_HINTS)


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
        "intent_parser": plan.intent_parser or "rule",
    }


def fallback_suspect_uses_import_line(issue_type: str) -> bool:
    """Localizer 降级定位是否优先 import 行。"""
    return resolve_prompt_routing(RepairPlan(issue_type=issue_type)).localizer_hints_key == "import_first"


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
