"""M5 System Prompt 模板单测。"""

from pathlib import Path

PROMPT_DIR = Path(__file__).parent.parent / "src" / "prompts"


def _read(name):
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


class TestPromptLoader:
    def test_load_system_prompt_matches_file(self):
        from src.prompts.loader import load_system_prompt

        assert load_system_prompt("localizer") == _read("localizer.txt")


class TestLocalizerPrompt:
    def test_contains_role(self):
        text = _read("localizer.txt")
        assert "定位专家" in text

    def test_lists_tools(self):
        text = _read("localizer.txt")
        for tool in ["ast_parse", "stack_parse", "read_file"]:
            assert tool in text

    def test_constrains_no_patch(self):
        text = _read("localizer.txt")
        assert "禁止跳过工具直接输出" in text


class TestRetrieverPrompt:
    def test_contains_role(self):
        text = _read("retriever.txt")
        assert "搜索专家" in text

    def test_lists_tools(self):
        text = _read("retriever.txt")
        for tool in ["search", "read_file", "git_blame", "find_test"]:
            assert tool in text


class TestPatcherPrompt:
    def test_contains_role(self):
        text = _read("patcher.txt")
        assert "补丁生成" in text

    def test_json_output_format(self):
        text = _read("patcher.txt")
        assert "original_lines" in text
        assert "patched_lines" in text
        assert "不要调工具" in text or "不要调任何工具" in text

    def test_type_error_fix_guidance_in_suffix(self):
        from src.prompts.loader import load_role_prompt

        text = load_role_prompt("patcher", "type_error")
        assert "int(a)" in text or "int()" in text
        assert "str(a)" in text or "str()" in text

    def test_forbids_tool_calls(self):
        text = _read("patcher.txt")
        assert "只输出上面的 JSON" in text

class TestIntentParserSnapshot:
    def test_intent_parser_in_snapshot(self):
        from src.repair.prompt_router import repair_plan_intent_snapshot
        from src.state import RepairPlan

        plan = RepairPlan(issue_type="type_error", intent_parser="rule")
        snap = repair_plan_intent_snapshot(plan)
        assert snap["intent_parser"] == "rule"
        assert snap["issue_type"] == "type_error"

    def test_intent_parser_defaults_to_rule(self):
        from src.repair.prompt_router import repair_plan_intent_snapshot
        from src.state import RepairPlan

        plan = RepairPlan(issue_type="unknown")
        snap = repair_plan_intent_snapshot(plan)
        assert snap["intent_parser"] == "rule"

class TestSkillConfidence:
    def test_confidence_high_priority_single_candidate(self):
        from src.skills.models import MatchedSkill

        m = MatchedSkill(
            name="python_type_error_fix", language="python",
            trigger_pattern="TypeError", priority=10,
            candidates_count=1,
        )
        assert m.confidence == 0.1
        assert "confidence" in m.to_trace_payload()

    def test_confidence_low_priority_many_candidates(self):
        from src.skills.models import MatchedSkill

        m = MatchedSkill(
            name="generic_fix", language="python",
            trigger_pattern="Error", priority=5,
            candidates_count=4,
        )
        assert m.confidence == round(0.05 / 4, 2)

    def test_confidence_in_intent_snapshot_with_match(self):
        from src.repair.prompt_router import repair_plan_intent_snapshot
        from src.state import RepairPlan
        from src.skills.models import MatchedSkill

        plan = RepairPlan(issue_type="type_error", intent_parser="rule")
        m = MatchedSkill(
            name="python_type_error_fix", language="python",
            trigger_pattern="TypeError", priority=10,
        )
        m.apply_to_plan(plan)
        snap = repair_plan_intent_snapshot(plan)
        assert snap["matched_skill"] == "python_type_error_fix"
        assert snap["skill_confidence"] == 0.1

    def test_confidence_zero_for_no_match(self):
        from src.repair.prompt_router import repair_plan_intent_snapshot
        from src.state import RepairPlan

        plan = RepairPlan(issue_type="unknown", intent_parser="rule")
        # skill context 默认 matched_skill=""
        snap = repair_plan_intent_snapshot(plan)
        assert snap["matched_skill"] is None
        assert snap["skill_confidence"] == 0.0
