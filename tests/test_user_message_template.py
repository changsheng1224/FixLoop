"""User Message 任务段模板单测。"""

from agent_runtime.user_message_template import (
    DEFAULT_TASK_TEMPLATE,
    load_task_template,
    render_task_message,
    render_template,
    template_fingerprint,
)


class TestRenderTemplate:
    def test_collapses_blank_optional_lines(self):
        text = render_template(
            "head\n$optional\n\ntail",
            {"optional": ""},
        )
        assert text == "head\n\ntail"

    def test_safe_substitute_leaves_unknown(self):
        text = render_template("$known $unknown", {"known": "ok"})
        assert text == "ok $unknown"


class TestRenderTaskMessage:
    def test_default_matches_legacy_header(self):
        rendered, meta = render_task_message("fix the bug")
        assert rendered == "## 当前任务\n\nfix the bug"
        assert meta["task_template_source"] == "builtin"
        assert len(meta["task_template_fingerprint"]) == 64

    def test_repo_override(self, temp_workspace):
        agent_dir = temp_workspace / ".agent"
        agent_dir.mkdir()
        custom = "## Task\n\n$task\n\n$refs"
        (agent_dir / "task_template.md").write_text(custom, encoding="utf-8")
        rendered, meta = render_task_message(
            "hello",
            repo_root=str(temp_workspace),
            refs="see README",
        )
        assert rendered == "## Task\n\nhello\n\nsee README"
        assert meta["task_template_source"] == "repo:.agent/task_template.md"
        assert meta["task_template_fingerprint"] == template_fingerprint(custom)

    def test_builtin_template_fingerprint_stable(self):
        _, source = load_task_template()
        assert source == "builtin"
        assert template_fingerprint(DEFAULT_TASK_TEMPLATE) == template_fingerprint(
            DEFAULT_TASK_TEMPLATE
        )
