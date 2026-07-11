"""Skill catalog, schema validation, and deterministic matching."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.skills.catalog import SkillCatalog, SkillCatalogError
from src.skills.matcher import match_skill
from src.skills.models import SkillSpec
from src.skills.prompt import format_skill_hint_block
from src.state import RepairPlan


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


class TestEvalSkillCoverage:
    """Match eval case issue snippets to expected skills."""

    @staticmethod
    def _match(issue: str):
        from src.skills.catalog import get_default_catalog

        get_default_catalog.cache_clear()
        return match_skill(issue, language="python")

    def test_case_001_type_error(self):
        issue = (
            "TypeError: unsupported operand type(s) for +: 'str' and 'int'\n"
            "File \"pricing.py\", line 6"
        )
        assert self._match(issue).name == "python_type_error_fix"

    def test_case_004_import_error(self):
        issue = "ModuleNotFoundError: No module named 'utils.helper'"
        assert self._match(issue).name == "python_import_error_fix"

    def test_case_006_logic_error(self):
        issue = "AssertionError: inclusive_range off-by-one"
        assert self._match(issue).name == "python_logic_error_fix"

    def test_case_007_attribute_error(self):
        issue = "AttributeError: 'NoneType' object has no attribute 'display_name'"
        assert self._match(issue).name == "python_attribute_error_fix"

    def test_case_009_config_error(self):
        issue = "KeyError: 'tool' — missing [tool.eval] in pyproject.toml"
        assert self._match(issue).name == "python_config_error_fix"

    def test_case_010_composite(self):
        issue = (
            "ModuleNotFoundError + TypeError (composite, two files)\n"
            "ModuleNotFoundError: No module named 'backend.service'\n"
            "TypeError on str + int"
        )
        assert self._match(issue).name == "python_composite_fix"

    def test_cannot_import_name_beats_generic_import(self):
        issue = "ImportError: cannot import name 'Foo' from 'pkg.module'"
        assert self._match(issue).name == "python_cannot_import_name_fix"

    def test_logic_beats_generic_test_failure(self):
        issue = "FAILED test_ranges.py::test_inclusive_range — off-by-one"
        assert self._match(issue).name == "python_logic_error_fix"


class TestSkillPrompt:
    def test_format_skill_hint_block(self):
        plan = RepairPlan(
            matched_skill="python_type_error_fix",
            suggested_tools=["stack_parse", "patch_file"],
            skill_example_issue="TypeError: bad op at foo.py:1",
            skill_guidance=["convert operands before arithmetic"],
            skill_avoid=["do not stringify numeric addition"],
            skill_example_patch="return int(a) + b",
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
        assert plan.matched_skill == "demo_skill"
        assert plan.skill_example_issue == "Error: demo"
        assert plan.skill_guidance == ["fix root cause"]
        assert plan.skill_avoid == ["no hacks"]
        assert plan.skill_example_patch == "patch line"


class TestSkillIntegration:
    def test_patcher_variables_include_skill_hint(self):
        from src.prompts.patcher_task_builder import assemble_patcher_variables

        plan = RepairPlan(
            matched_skill="python_type_error_fix",
            suggested_tools=["stack_parse", "patch_file"],
            skill_guidance=["convert before add"],
            skill_example_patch="cast operands",
        )
        variables = assemble_patcher_variables(
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
            matched_skill="python_type_error_fix",
            suggested_tools=["stack_parse"],
        )
        snapshot = repair_plan_intent_snapshot(plan)
        assert snapshot["matched_skill"] == "python_type_error_fix"
        assert snapshot["suggested_tools"] == ["stack_parse"]
