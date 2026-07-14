"""Loop limits 单测。"""

from agent_runtime.loop_limits import NATIVE_MAX_TURNS_MESSAGE, max_parse_attempts


class TestMaxParseAttempts:
    def test_default(self):
        assert max_parse_attempts(6) == 22  # 6*3 + 4

    def test_min_steps(self):
        assert max_parse_attempts(1) == 7  # 1*3 + 4

    def test_high_steps(self):
        assert max_parse_attempts(50) == 154


class TestNativeMaxTurns:
    def test_message_constant(self):
        assert NATIVE_MAX_TURNS_MESSAGE == "max_turns exceeded"
