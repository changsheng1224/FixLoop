"""单步 wall-clock 超时：monotonic deadline + 阶段边界检查。"""

from __future__ import annotations

import time

__all__ = ["StepClock", "StepTimeoutError"]


class StepTimeoutError(Exception):
    """ReAct 单步超过 step_timeout_s。"""

    def __init__(self, timeout_s: int, *, step: int, path: str = ""):
        self.timeout_s = int(timeout_s)
        self.step = int(step)
        self.path = path or ""
        super().__init__(
            f"step {self.step} timed out after {self.timeout_s}s"
            + (f" ({self.path})" if self.path else "")
        )


class StepClock:
    """一步的单调时钟；timeout_s≤0 时禁用。"""

    def __init__(self, timeout_s: int):
        self.timeout_s = int(timeout_s)
        if self.timeout_s > 0:
            self._started = time.monotonic()
            self._deadline = self._started + self.timeout_s
        else:
            self._started = None
            self._deadline = None

    @property
    def enabled(self) -> bool:
        return self._deadline is not None

    def elapsed_ms(self) -> int:
        if not self.enabled or self._started is None:
            return 0
        return int((time.monotonic() - self._started) * 1000)

    def expired(self) -> bool:
        return self.enabled and time.monotonic() >= self._deadline

    def check(self, *, step: int, path: str = "") -> None:
        if self.expired():
            raise StepTimeoutError(self.timeout_s, step=step, path=path)
