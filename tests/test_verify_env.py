"""可跑环境：Django runtests 探测与命令构造。"""

from __future__ import annotations

from pathlib import Path

from src.harness.verify_env import (
    build_django_runtests_command,
    build_verify_env_prefix,
    detect_verify_profile,
    django_labels_from_target,
    parse_django_runtests_output,
)


def _django_tree(tmp: Path) -> Path:
    (tmp / "django").mkdir()
    (tmp / "django" / "__init__.py").write_text("", encoding="utf-8")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "runtests.py").write_text("print('run')\n", encoding="utf-8")
    (tmp / "tests" / "test_sqlite.py").write_text("SECRET_KEY='x'\n", encoding="utf-8")
    return tmp


class TestDetectProfile:
    def test_django_runtests_detected(self, tmp_path: Path):
        root = _django_tree(tmp_path)
        prof = detect_verify_profile(root)
        assert prof.kind == "django_runtests"
        assert prof.settings_module == "tests.test_sqlite"
        assert "runtests.py" in prof.runtests_path

    def test_default_pytest(self, tmp_path: Path):
        (tmp_path / "pkg").mkdir()
        prof = detect_verify_profile(tmp_path)
        assert prof.kind == "pytest"


class TestLabelsAndCommand:
    def test_labels_from_pytest_nodeid(self):
        labels = django_labels_from_target("tests/queries/tests.py::test_union")
        assert labels == ["queries.tests.test_union"]

    def test_empty_target_no_labels(self):
        assert django_labels_from_target(".") == []
        assert django_labels_from_target("") == []

    def test_command_includes_settings_and_label(self):
        cmd = build_django_runtests_command(
            ["queries.tests"],
            settings_module="tests.test_sqlite",
        )
        assert "python tests/runtests.py" in cmd
        assert "--settings=tests.test_sqlite" in cmd
        assert "queries.tests" in cmd
        assert ";rm" not in build_django_runtests_command(["x;rm"])


class TestEnvPrefix:
    def test_includes_django_settings(self, tmp_path: Path):
        root = _django_tree(tmp_path)
        prof = detect_verify_profile(root)
        prefix = build_verify_env_prefix(root, prof)
        assert "PYTHONPATH" in prefix
        assert 'DJANGO_SETTINGS_MODULE="tests.test_sqlite"' in prefix


class TestParseRuntests:
    def test_ok(self):
        out = "Ran 3 tests in 0.1s\n\nOK\n"
        vr = parse_django_runtests_output(out, exit_code=0)
        assert vr.all_passed
        assert vr.total_tests == 3
        assert vr.failed == 0

    def test_failed(self):
        out = "Ran 2 tests in 0.1s\n\nFAILED (failures=1)\n"
        vr = parse_django_runtests_output(out, exit_code=1)
        assert not vr.all_passed
        assert vr.total_tests == 2
        assert vr.failed == 1

    def test_zero_with_error_is_verify_config(self):
        out = "ImproperlyConfigured: settings are not configured\n"
        vr = parse_django_runtests_output(out, exit_code=1, labels=["queries"])
        assert not vr.all_passed
        assert vr.total_tests == 0
        assert any("verify_config" in x for x in vr.failure_logs)
