"""Skill catalog, schema validation, and deterministic matching."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.skills.catalog import SkillCatalog, SkillCatalogError
from src.skills.matcher import match_skill
from src.skills.models import SkillSpec
from src.skills.prompt import format_skill_hint_block
from src.state import RepairPlan, SkillContext


class TestSkillSpec:
    def test_valid_spec(self):
        spec = SkillSpec(
            name="demo",
            language="python",
            trigger_pattern="TypeError",
            priority=10,
            suggested_tools=["search"],
            example_patch="fix types",
        )
        assert spec.matches("TypeError: bad op")

    def test_invalid_regex_rejected(self):
        with pytest.raises(ValidationError):
            SkillSpec(name="bad", trigger_pattern="[unclosed")


class TestSkillCatalog:
    def test_loads_yaml_files(self, tmp_path: Path):
        (tmp_path / "a.yaml").write_text(
            "name: skill_a\nlanguage: python\ntrigger_pattern: TypeError\npriority: 10\n",
            encoding="utf-8",
        )
        catalog = SkillCatalog.load_from_directory(tmp_path)
        assert len(catalog.skills) == 1
        assert catalog.skills[0].name == "skill_a"

    def test_invalid_yaml_raises(self, tmp_path: Path):
        (tmp_path / "bad.yaml").write_text("name: missing_pattern\n", encoding="utf-8")
        with pytest.raises(SkillCatalogError):
            SkillCatalog.load_from_directory(tmp_path)

    def test_duplicate_name_raises(self, tmp_path: Path):
        (tmp_path / "a.yaml").write_text(
            "name: dup\ntrigger_pattern: A\n",
            encoding="utf-8",
        )
        (tmp_path / "b.yaml").write_text(
            "name: dup\ntrigger_pattern: B\n",
            encoding="utf-8",
        )
        with pytest.raises(SkillCatalogError):
            SkillCatalog.load_from_directory(tmp_path)


class TestMatchSkill:
    def _write(self, directory: Path, filename: str, body: str) -> None:
        (directory / filename).write_text(body, encoding="utf-8")

    def test_priority_and_longest_pattern(self, tmp_path: Path):
        self._write(
            tmp_path,
            "low.yaml",
            "name: low\ntrigger_pattern: Error\npriority: 1\n",
        )
        self._write(
            tmp_path,
            "high_short.yaml",
            "name: high_short\ntrigger_pattern: Error\npriority: 10\n",
        )
        self._write(
            tmp_path,
            "high_long.yaml",
            "name: high_long\ntrigger_pattern: TypeError\npriority: 10\n",
        )
        catalog = SkillCatalog.load_from_directory(tmp_path)
        matched = match_skill("TypeError: boom", language="python", catalog=catalog)
        assert matched is not None
        assert matched.name == "high_long"

    def test_language_filter(self, tmp_path: Path):
        self._write(
            tmp_path,
            "py.yaml",
            "name: py\ntrigger_pattern: Error\nlanguage: python\n",
        )
        self._write(
            tmp_path,
            "js.yaml",
            "name: js\ntrigger_pattern: Error\nlanguage: javascript\n",
        )
        catalog = SkillCatalog.load_from_directory(tmp_path)
        assert match_skill("Error", language="python", catalog=catalog).name == "py"
        assert match_skill("Error", language="javascript", catalog=catalog).name == "js"

    def test_no_match_returns_none(self, tmp_path: Path):
        self._write(tmp_path, "a.yaml", "name: a\ntrigger_pattern: FooError\n")
        catalog = SkillCatalog.load_from_directory(tmp_path)
        assert match_skill("TypeError", catalog=catalog) is None

    def test_builtin_type_error_skill(self):
        matched = match_skill(
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
            language="python",
        )
        assert matched is not None
        assert matched.name == "python_type_error_fix"
        assert "stack_parse" in matched.suggested_tools

    def test_builtin_catalog_has_ten_skills(self):
        from src.skills.catalog import get_default_catalog

        get_default_catalog.cache_clear()
        assert len(get_default_catalog().skills) == 10


class TestSkillPrompt:
    def test_format_skill_hint_block(self):
        plan = RepairPlan(
            skill=SkillContext(
                matched_skill="python_type_error_fix",
                suggested_tools=["stack_parse", "patch_file"],
                example_issue="TypeError: bad op at foo.py:1",
                guidance=["convert operands before arithmetic"],
                avoid=["do not stringify numeric addition"],
                example_patch="return int(a) + b",
            ),
        )
        block = format_skill_hint_block(plan)
        assert "[Skill 提示]" in block
        assert "python_type_error_fix" in block
        assert "stack_parse" in block
        assert "参考 issue:" in block
        assert "TypeError: bad op" in block
        assert "修复原则:" in block
        assert "convert operands" in block
        assert "避免:" in block
        assert "stringify numeric" in block
        assert "示例修复: return int(a) + b" in block

    def test_empty_when_no_match(self):
        assert format_skill_hint_block(RepairPlan()) == ""


class TestMatchedSkillApply:
    def test_apply_to_plan_copies_all_fields(self):
        from src.skills.models import MatchedSkill

        matched = MatchedSkill(
            name="demo_skill",
            language="python",
            trigger_pattern="Error",
            priority=1,
            suggested_tools=["read_file"],
            example_issue="Error: demo",
            guidance=["fix root cause"],
            avoid=["no hacks"],
            example_patch="patch line",
        )
        plan = RepairPlan()
        matched.apply_to_plan(plan)
        assert plan.skill.matched_skill == "demo_skill"
        assert plan.skill.example_issue == "Error: demo"
        assert plan.skill.guidance == ["fix root cause"]
        assert plan.skill.avoid == ["no hacks"]
        assert plan.skill.example_patch == "patch line"


class TestSkillIntegration:
    def test_patcher_variables_include_skill_hint(self):
        from src.prompts.patcher_task_builder import assemble_patcher_variables

        plan = RepairPlan(
            skill=SkillContext(
                matched_skill="python_type_error_fix",
                suggested_tools=["stack_parse", "patch_file"],
                guidance=["convert before add"],
                example_patch="cast operands",
            ),
        )
        variables, _, _ = assemble_patcher_variables(
            suspects=[],
            context=None,
            feedback="",
            plan=plan,
            issue="TypeError: bad",
            read_snippet=lambda *_: "",
            read_test_context=lambda *_: [],
            fallback_suspects=lambda *_: [],
        )
        assert "[Skill 提示]" in variables["skill_hint_block"]
        assert "python_type_error_fix" in variables["skill_hint_block"]
        assert "修复原则:" in variables["skill_hint_block"]

    def test_intent_snapshot_exports_matched_skill(self):
        from src.repair.prompt_router import repair_plan_intent_snapshot

        plan = RepairPlan(
            issue_type="type_error",
            skill=SkillContext(
                matched_skill="python_type_error_fix",
                suggested_tools=["stack_parse"],
            ),
        )
        snapshot = repair_plan_intent_snapshot(plan)
        assert snapshot["matched_skill"] == "python_type_error_fix"
        assert snapshot["suggested_tools"] == ["stack_parse"]
        assert snapshot["skill"]["matched_skill"] == "python_type_error_fix"
