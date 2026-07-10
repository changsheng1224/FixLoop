"""Patcher issue-type prompt 变体单测。"""

from pathlib import Path

PROMPT_DIR = Path(__file__).parent.parent / "src" / "prompts"
SUFFIX_DIR = PROMPT_DIR / "patcher_suffix"


def _read(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _read_suffix(name: str) -> str:
    return (SUFFIX_DIR / name).read_text(encoding="utf-8")


class TestLoadRolePrompt:
    def test_patcher_base_only_without_issue_type(self):
        from src.prompts.loader import load_role_prompt

        assert load_role_prompt("patcher", "") == _read("patcher.txt").strip()

    def test_patcher_type_error_suffix(self):
        from src.prompts.loader import load_role_prompt

        text = load_role_prompt("patcher", "type_error")
        assert "补丁生成" in text
        assert "TypeError" in text
        assert "int(a)" in text

    def test_patcher_import_error_suffix(self):
        from src.prompts.loader import load_role_prompt

        text = load_role_prompt("patcher", "import_error")
        assert "ImportError" in text
        assert "utils.helper" in text

    def test_patcher_logic_error_suffix(self):
        from src.prompts.loader import load_role_prompt

        text = load_role_prompt("patcher", "logic_error")
        assert "off-by-one" in text

    def test_patcher_unknown_falls_back_to_default(self):
        from src.prompts.loader import load_role_prompt

        text = load_role_prompt("patcher", "syntax_error")
        assert "通用修复原则" in text
        assert _read_suffix("default.txt").strip() in text

    def test_non_patcher_role_ignores_issue_type(self):
        from src.prompts.loader import load_role_prompt, load_system_prompt

        assert load_role_prompt("localizer", "type_error") == load_system_prompt("localizer").strip()


class TestLoadLocalizerHints:
    def test_import_first_hints(self):
        from src.prompts.loader import load_localizer_hints

        text = load_localizer_hints("import_first")
        assert "import 错误" in text
        assert "stack_parse" in text

    def test_stack_first_default(self):
        from src.prompts.loader import load_localizer_hints

        text = load_localizer_hints("stack_first")
        assert "stack_parse" in text
        assert "ast_parse" in text

    def test_unknown_key_falls_back_to_stack_first(self):
        from src.prompts.loader import load_localizer_hints

        text = load_localizer_hints("missing_key")
        assert "stack_parse" in text


class TestLoadPatcherUserHints:
    def test_cannot_import_name_hint(self):
        from src.prompts.loader import load_patcher_user_hint

        text = load_patcher_user_hint("cannot_import_name")
        assert "错误符号名" in text

    def test_composite_hint_format(self):
        from src.prompts.loader import load_patcher_user_hint

        text = load_patcher_user_hint("composite", file_count=3)
        assert "3" in text

class TestPatcherPromptDedup:
    def test_user_prompt_no_type_error_system_hints(self, temp_workspace):
        from src.orchestrator import Orchestrator
        from src.state import RepairPlan, SuspectLocation

        orch = Orchestrator(None, None, None)
        orch._repo_root = str(temp_workspace)
        plan = RepairPlan(issue_type="type_error")
        prompt, _ = orch._patcher_prompt(
            [SuspectLocation(file_path="calc.py", start_line=1, end_line=2, reason="")],
            None,
            plan=plan,
        )
        assert "修复提示: 这是类型错误" not in prompt
        assert "请用 int()/float() 做数值转换" not in prompt

    def test_user_prompt_keeps_issue_specific_heuristics(self):
        from src.orchestrator import Orchestrator
        from src.state import RepairPlan

        orch = Orchestrator(None, None, None)
        plan = RepairPlan(issue_type="import_error", suspect_files=["app.py"])
        issue = "ImportError: cannot import name 'foo' from 'bar'"
        prompt, _ = orch._patcher_prompt([], None, plan=plan, issue=issue)
        assert "cannot import name" in prompt.lower() or "错误符号名" in prompt
