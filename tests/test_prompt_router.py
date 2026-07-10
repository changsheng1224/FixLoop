"""issue_type → prompt 变体路由单测。"""

import pytest

from src.repair.prompt_router import apply_prompt_routing, resolve_prompt_routing
from src.state import RepairPlan


@pytest.mark.parametrize(
    ("issue_type", "patcher", "localizer"),
    [
        ("type_error", "type_error", "stack_first"),
        ("import_error", "import_error", "import_first"),
        ("attribute_error", "attribute_error", "stack_first"),
        ("logic_error", "logic_error", "stack_first"),
        ("config_error", "config_error", "stack_first"),
        ("composite", "composite", "stack_first"),
        ("test_failure", "default", "stack_first"),
        ("value_error", "type_error", "stack_first"),
        ("syntax_error", "default", "stack_first"),
        ("unknown", "default", "stack_first"),
        ("", "default", "stack_first"),
    ],
)
def test_resolve_prompt_routing_table(issue_type, patcher, localizer):
    plan = RepairPlan(issue_type=issue_type)
    routing = resolve_prompt_routing(plan)
    assert routing.source_issue_type == issue_type
    assert routing.patcher_variant == patcher
    assert routing.localizer_hints_key == localizer


def test_apply_prompt_routing_writes_plan():
    plan = RepairPlan(issue_type="import_error")
    routing = apply_prompt_routing(plan)
    assert routing.patcher_variant == "import_error"
    assert plan.prompt_variants == {
        "patcher": "import_error",
        "localizer": "import_first",
    }


def test_trace_payload_shape():
    plan = RepairPlan(issue_type="type_error")
    payload = resolve_prompt_routing(plan).to_trace_payload()
    assert payload["issue_type"] == "type_error"
    assert payload["prompt_variants"]["patcher"] == "type_error"
    assert payload["prompt_variants"]["localizer"] == "stack_first"
