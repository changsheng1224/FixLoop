"""language_detect 单测。"""

from src.repair.language_detect import DEFAULT_LANGUAGE, detect_repair_language


class TestDetectRepairLanguage:
    def test_python_from_py_extension(self):
        lang, source = detect_repair_language(
            "TypeError at calculator.py:42",
            suspect_files=["calculator.py"],
        )
        assert lang == "python"
        assert source.startswith("extension:")

    def test_java_from_stack(self):
        issue = (
            'Exception in thread "main" java.lang.NullPointerException\n'
            "    at com.example.Bar.main(Bar.java:10)"
        )
        lang, source = detect_repair_language(issue)
        assert lang == "java"
        assert "extension:.java" in source or source.startswith("keyword:")

    def test_go_from_file_and_panic(self):
        lang, source = detect_repair_language("panic: runtime error at main.go:42")
        assert lang == "go"
        assert source

    def test_explicit_lang_tag(self):
        lang, source = detect_repair_language("[lang:javascript] bundle failed")
        assert lang == "javascript"
        assert source == "explicit"

    def test_default_without_signals(self):
        lang, source = detect_repair_language("the app behaves oddly after deploy")
        assert lang == DEFAULT_LANGUAGE
        assert source == "default"

    def test_shebang_from_repo_file(self, tmp_path):
        script = tmp_path / "run.py"
        script.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
        lang, source = detect_repair_language(
            "execution failed",
            suspect_files=["run.py"],
            repo_root=tmp_path,
        )
        assert lang == "python"
        assert source == "shebang:file"

    def test_explicit_overrides_extension(self):
        lang, _source = detect_repair_language(
            "[lang:go] TypeError at calculator.py:1",
            suspect_files=["calculator.py"],
        )
        assert lang == "go"
