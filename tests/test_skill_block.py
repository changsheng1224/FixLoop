"""Canonical Skill block renderer tests."""

from __future__ import annotations

from src.repair.prompt_router import apply_prompt_routing
from src.skills.skill_block import (
    ROLE_LABELS,
    SKILL_BLOCK_HEADER,
    render_skill_hint_for_plan,
    skill_hint_rendered_trace,
)
from src.state import RepairPlan, SkillContext


def _sample_plan() -> RepairPlan:
    plan = RepairPlan(
        issue_type="type_error",
        suspect_files=["foo.py"],
        skill=SkillContext(
            matched_skill="python_type_error_fix",
            suggested_tools=["stack_parse", "ast_parse", "search", "patch_file"],
            example_issue="TypeError: bad op",
            guidance=[
                "convert operands before arithmetic",
                "check stack trace first",
                "third line trimmed for retriever",
            ],
            avoid=["do not stringify numeric addition"],
            example_patch="return int(a) + b",
        ),
    )
    apply_prompt_routing(plan)
    return plan


class TestRenderSkillHintForPlan:
    def test_unified_header_for_all_roles(self):
        plan = _sample_plan()
        for role in ("localizer", "retriever", "patcher", "verifier"):
            render = render_skill_hint_for_plan(plan, role)
            assert render.source == "hit"
            assert render.text.startswith(SKILL_BLOCK_HEADER)
            assert f"角色: {ROLE_LABELS[role]}" in render.text

    def test_localizer_projection(self):
        render = render_skill_hint_for_plan(_sample_plan(), "localizer")
        assert "工具序:" in render.text
        assert "定位提示:" in render.text
        assert "修复原则:" not in render.text

    def test_retriever_projection_limits_guidance(self):
        render = render_skill_hint_for_plan(_sample_plan(), "retriever")
        assert "检索要点:" in render.text
        assert "convert operands" in render.text
        assert "third line trimmed" not in render.text
        # E3′: ACL 外工具不得出现在工具序
        assert "stack_parse" not in render.text
        assert "patch_file" not in render.text
        assert "search" in render.text

    def test_patcher_full_projection(self):
        render = render_skill_hint_for_plan(_sample_plan(), "patcher")
        assert "修复原则:" in render.text
        assert "避免:" in render.text
        assert "示例修复:" in render.text

    def test_verifier_light_projection(self):
        render = render_skill_hint_for_plan(_sample_plan(), "verifier")
        assert "验证要点:" in render.text
        assert "避免:" in render.text
        assert "工具序:" not in render.text

    def test_miss_uses_generic_strategy(self):
        plan = RepairPlan(
            issue_type="type_error",
            skill=SkillContext(fallback_strategy="issue_type_routing"),
        )
        render = render_skill_hint_for_plan(plan, "patcher")
        assert render.source == "miss"
        assert "策略: generic" in render.text
        assert "要点:" in render.text

    def test_none_when_no_plan_or_fallback(self):
        assert render_skill_hint_for_plan(None, "patcher").source == "none"
        assert render_skill_hint_for_plan(RepairPlan(), "patcher").source == "none"

    def test_trace_payload(self):
        render = render_skill_hint_for_plan(_sample_plan(), "localizer")
        payload = skill_hint_rendered_trace(render)
        assert payload == {"role": "localizer", "source": "hit", "char_len": len(render.text)}
