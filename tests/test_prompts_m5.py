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
        for tool in ["ast_parse", "stack_parse", "read_file", "search", "git_blame"]:
            assert tool in text

    def test_constrains_no_patch(self):
        text = _read("localizer.txt")
        assert "不要修改代码" in text or "不要生成补丁" in text


class TestRetrieverPrompt:
    def test_contains_role(self):
        text = _read("retriever.txt")
        assert "搜索专家" in text

    def test_lists_tools(self):
        text = _read("retriever.txt")
        for tool in ["search", "read_file", "git_blame", "git_diff", "find_test"]:
            assert tool in text


class TestPatcherPrompt:
    def test_contains_role(self):
        text = _read("patcher.txt")
        assert "补丁生成" in text

    def test_lists_tools(self):
        text = _read("patcher.txt")
        for tool in ["read_file", "write_file", "patch_file"]:
            assert tool in text

    def test_forbids_self_localization(self):
        text = _read("patcher.txt")
        assert "不要重新定位" in text
        assert "定位已由 Localizer 完成" in text
