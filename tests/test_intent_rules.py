"""Tests for rule-layer intent classification."""

from agent_runtime.intent.rules import classify_rules, is_constraint_text


class TestClassifyRules:
    def test_slash_help(self):
        hit = classify_rules("/help")
        assert hit.primary == "help"
        assert hit.confidence >= 0.99

    def test_slash_cancel(self):
        hit = classify_rules("/cancel")
        assert hit.primary == "cancel"
        assert hit.action == "noop_cancel"

    def test_remember(self):
        hit = classify_rules("请记住默认测试工具是 pytest")
        assert hit.primary == "remember"
        assert hit.action == "promote_memory"

    def test_repair_request(self):
        hit = classify_rules("帮我修这个 TypeError")
        assert hit.primary == "repair_request"
        assert hit.slots.get("issue_type") == "type_error"

    def test_repair_channel_stack(self):
        text = (
            'Traceback (most recent call last):\n'
            '  File "calculator.py", line 42, in add\n'
            "TypeError: unsupported operand"
        )
        hit = classify_rules(text, channel="repair")
        assert hit.primary == "repair_issue"
        assert "calculator.py" in hit.slots.get("suspect_files", [])

    def test_default_ask(self):
        hit = classify_rules("配置里 timeout 该怎么设？")
        assert hit.primary == "ask"

    def test_explain(self):
        hit = classify_rules("解释一下 AgentLoop")
        assert hit.primary == "explain"

    def test_review(self):
        hit = classify_rules("帮我看看有没有问题")
        assert hit.primary == "review"

    def test_refactor(self):
        hit = classify_rules("重构一下这个函数")
        assert hit.primary == "refactor"

    def test_constraint_text(self):
        assert is_constraint_text("只用改 foo.py")
        hit = classify_rules("只用改 foo.py")
        assert hit.reason == "rule:constraint"
        assert "foo.py" in hit.slots.get("suspect_files", [])

    def test_empty_clarify(self):
        hit = classify_rules("  ")
        assert hit.primary == "clarify"
