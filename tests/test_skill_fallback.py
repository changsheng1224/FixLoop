"""Skill miss fallback routing tests."""

from __future__ import annotations

from src.repair.prompt_router import apply_prompt_routing
from src.skills.fallback import (
    GENERIC_FALLBACK_ISSUE_TYPES,
    apply_skill_fallback,
    resolve_skill_fallback,
    skill_matched_trace_payload,
)
from src.skills.prompt import format_skill_hint, format_skill_hint_for_plan
from src.state import RepairPlan


class TestResolveSkillFallback:
    def test_hit_when_matched_skill_set(self):
        plan = RepairPlan(issue_type="type_error", matched_skill="python_type_error_fix")
        apply_prompt_routing(plan)
        fb = resolve_skill_fallback(plan)
        assert fb.strategy == "hit"
        assert fb.inject_miss_hint is False

    def test_issue_type_routing_for_type_error_miss(self):
        plan = RepairPlan(issue_type="type_error")
        apply_prompt_routing(plan)
        fb = resolve_skill_fallback(plan)
        assert fb.strategy == "issue_type_routing"
        assert fb.patcher_variant == "type_error"
        assert fb.inject_miss_hint is True

    def test_generic_patcher_for_unknown(self):
        plan = RepairPlan(issue_type="unknown")
        apply_prompt_routing(plan)
        fb = resolve_skill_fallback(plan)
        assert fb.strategy == "generic_patcher"
        assert fb.patcher_variant == "default"

    def test_generic_patcher_for_test_failure(self):
        plan = RepairPlan(issue_type="test_failure")
        apply_prompt_routing(plan)
        fb = resolve_skill_fallback(plan)
        assert fb.strategy == "generic_patcher"
        assert "test_failure" in GENERIC_FALLBACK_ISSUE_TYPES


class TestApplySkillFallback:
    def test_generic_overrides_patcher_variant(self):
        plan = RepairPlan(issue_type="unknown")
        apply_prompt_routing(plan)
        assert plan.prompt_variants["patcher"] == "default"
        fb = apply_skill_fallback(plan, matched=None)
        assert fb.strategy == "generic_patcher"
        assert plan.skill_fallback_strategy == "generic_patcher"
        assert plan.prompt_variants["patcher"] == "default"

    def test_issue_type_routing_keeps_variant(self):
        plan = RepairPlan(issue_type="import_error")
        apply_prompt_routing(plan)
        fb = apply_skill_fallback(plan, matched=None)
        assert fb.strategy == "issue_type_routing"
        assert plan.prompt_variants["patcher"] == "import_error"
        assert plan.skill_fallback_strategy == "issue_type_routing"

    def test_hit_does_not_change_variant(self):
        plan = RepairPlan(issue_type="type_error", matched_skill="python_type_error_fix")
        apply_prompt_routing(plan)
        apply_skill_fallback(plan, matched=object())
        assert plan.skill_fallback_strategy == "hit"
        assert plan.prompt_variants["patcher"] == "type_error"


class TestSkillMatchedTracePayload:
    def test_miss_payload(self):
        plan = RepairPlan(issue_type="unknown")
        apply_prompt_routing(plan)
        fb = apply_skill_fallback(plan, matched=None)
        payload = skill_matched_trace_payload(None, fb)
        assert payload["matched_skill"] is None
        assert payload["fallback_strategy"] == "generic_patcher"
        assert payload["patcher_variant"] == "default"
        assert payload["inject_miss_hint"] is True

    def test_hit_payload_includes_strategy(self):
        from src.skills.models import MatchedSkill

        matched = MatchedSkill(
            name="demo",
            language="python",
            trigger_pattern="Error",
            priority=1,
        )
        plan = RepairPlan(matched_skill="demo")
        fb = resolve_skill_fallback(plan)
        payload = skill_matched_trace_payload(matched, fb)
        assert payload["matched_skill"] == "demo"
        assert payload["fallback_strategy"] == "hit"


class TestSkillMissHints:
    def test_format_skill_hint_still_empty_without_match(self):
        assert format_skill_hint(RepairPlan(), "localizer") == ""

    def test_format_skill_hint_for_plan_miss_localizer(self):
        plan = RepairPlan(issue_type="type_error", skill_fallback_strategy="issue_type_routing")
        block = format_skill_hint_for_plan(plan, "localizer")
        assert "[Skill 提示·通用]" in block

    def test_format_skill_hint_for_plan_hit_uses_skill_block(self):
        plan = RepairPlan(
            issue_type="type_error",
            matched_skill="python_type_error_fix",
            suggested_tools=["stack_parse"],
            skill_guidance=["convert operands"],
            skill_fallback_strategy="hit",
        )
        block = format_skill_hint_for_plan(plan, "patcher")
        assert "[Skill 提示]" in block
        assert "python_type_error_fix" in block
        assert "通用" not in block

    def test_no_miss_hint_when_strategy_unset(self):
        plan = RepairPlan(issue_type="type_error")
        assert format_skill_hint_for_plan(plan, "patcher") == ""
