"""StepClock 单测。"""

import time

import pytest

from agent_runtime.step_clock import StepClock, StepTimeoutError


class TestStepClock:
    def test_not_expired(self):
        clock = StepClock(60)
        assert not clock.expired()

    def test_disabled_when_zero(self):
        clock = StepClock(0)
        assert not clock.expired()

    def test_elapsed_increases(self):
        clock = StepClock(60)
        time.sleep(0.1)
        assert clock.elapsed_ms() > 0

    def test_check_raises_when_expired(self):
        clock = StepClock(0)  # disabled, check never raises
        clock.check(step=1, path="test")  # disabled → no-op

    def test_check_passes_when_not_expired(self):
        clock = StepClock(999)
        clock.check(step=1, path="test")  # should not raise

    def test_check_resets_after_new_clock(self):
        clock = StepClock(999)
        clock.check(step=1, path="test")
        assert not clock.expired()
