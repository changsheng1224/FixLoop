"""Role-specific Skill prompt injection tests."""

from __future__ import annotations

from src.prompts.repair_tasks import build_localizer_variables, build_retriever_template_and_variables
from src.repair.prompt_router import apply_prompt_routing
from src.skills.prompt import format_skill_hint
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


class TestFormatSkillHintRoles:
    def test_localizer_is_tools_focused(self):
        block = format_skill_hint(_sample_plan(), "localizer")
        assert "[Skill 提示]" in block
        assert "角色: Localizer" in block
        assert "工具序:" in block
        assert "stack_parse" in block
        assert "修复原则:" not in block
        assert "示例修复:" not in block

    def test_retriever_includes_limited_guidance(self):
        block = format_skill_hint(_sample_plan(), "retriever")
        assert "[Skill 提示]" in block
        assert "角色: Retriever" in block
        assert "convert operands" in block
        assert "third line trimmed" not in block
        assert "避免:" not in block

    def test_patcher_is_full_block(self):
        block = format_skill_hint(_sample_plan(), "patcher")
        assert "[Skill 提示]" in block
        assert "角色: Patcher" in block
        assert "修复原则:" in block
        assert "避免:" in block
        assert "示例修复:" in block

    def test_empty_when_unmatched(self):
        assert format_skill_hint(RepairPlan(), "localizer") == ""


class TestSkillHintInTaskVariables:
    def test_localizer_variables_include_skill_block(self):
        variables = build_localizer_variables(_sample_plan(), issue="TypeError")
        assert "[Skill 提示]" in variables["skill_hint_block"]
        assert "角色: Localizer" in variables["skill_hint_block"]

    def test_retriever_variables_include_skill_block(self):
        _, variables = build_retriever_template_and_variables([], plan=_sample_plan())
        assert "[Skill 提示]" in variables["skill_hint_block"]
        assert "角色: Retriever" in variables["skill_hint_block"]
