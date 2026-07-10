"""issue_type → L2 prompt 变体集中路由。"""

from __future__ import annotations

from dataclasses import dataclass

from src.state import RepairPlan

_DEFAULT_PATCHER_VARIANT = "default"
_DEFAULT_LOCALIZER_HINTS = "stack_first"

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
