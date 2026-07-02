"""M5 System Prompt 模板单测。"""

from pathlib import Path

PROMPT_DIR = Path(__file__).parent.parent / "src" / "prompts"


def _read(name):
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


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

    def test_forbids_tool_calls(self):
        text = _read("patcher.txt")
        assert "只输出上面的 JSON" in text
