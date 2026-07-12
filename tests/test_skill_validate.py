"""Tests for Skill YAML schema validation (L1 + L2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skills.catalog import SkillCatalog, SkillCatalogError
from src.skills.validate import validate_directory


class TestSkillValidateL1:
    def test_unknown_tool_is_error(self, tmp_path: Path):
        (tmp_path / "bad.yaml").write_text(
            "\n".join(
                [
                    "name: bad_skill",
                    "trigger_pattern: Error",
                    "suggested_tools: [not_a_real_tool]",
                    "guidance:",
                    "  - fix it",
                ]
            ),
            encoding="utf-8",
        )
        report = validate_directory(tmp_path)
        assert not report.ok
        assert any("unknown suggested_tools" in issue.message for issue in report.errors)

    def test_empty_guidance_is_error(self, tmp_path: Path):
        (tmp_path / "bad.yaml").write_text(
            "name: bad_skill\ntrigger_pattern: Error\nguidance: []\n",
            encoding="utf-8",
        )
        report = validate_directory(tmp_path)
        assert not report.ok

    def test_invalid_name_slug_is_error(self, tmp_path: Path):
        (tmp_path / "bad.yaml").write_text(
            "name: Bad-Skill\ntrigger_pattern: Error\nguidance:\n  - x\n",
            encoding="utf-8",
        )
        report = validate_directory(tmp_path)
        assert not report.ok


class TestSkillValidateL2:
    def test_duplicate_name_is_error(self, tmp_path: Path):
        body = "name: dup\ntrigger_pattern: A\npriority: 1\nguidance:\n  - a\n"
        (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
        (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
        report = validate_directory(tmp_path)
        assert not report.ok
        assert any(issue.field == "name" for issue in report.errors)

    def test_filename_mismatch_is_warning(self, tmp_path: Path):
        (tmp_path / "file_a.yaml").write_text(
            "name: skill_b\ntrigger_pattern: Error\nguidance:\n  - hint\n",
            encoding="utf-8",
        )
        report = validate_directory(tmp_path)
        assert report.ok
        assert any(issue.field == "name" for issue in report.warnings)

    def test_builtin_skills_validate_clean(self):
        from src.skills.catalog import _BUILTIN_DIR

        report = validate_directory(_BUILTIN_DIR)
        assert report.ok
        assert report.skill_count == 11
        assert not report.warnings  # filenames match skill names after P2-11 rename

    def test_strict_load_raises_on_errors(self, tmp_path: Path):
        (tmp_path / "bad.yaml").write_text(
            "name: bad\ntrigger_pattern: Error\nguidance: []\n",
            encoding="utf-8",
        )
        with pytest.raises(SkillCatalogError):
            SkillCatalog.load_from_directory(tmp_path)


class TestSkillsCliValidate:
    def test_cli_validate_builtin(self, monkeypatch, capsys):
        import sys

        monkeypatch.setattr(
            sys,
            "argv",
            ["src.cli", "skills", "validate"],
        )
        from src.cli import main

        code = main()
        assert code == 0
        out = capsys.readouterr().out
        assert "OK 11 skill(s)" in out
