"""Shared session usage and per-call timing fields for ModelClient implementations."""

from __future__ import annotations

from agent_runtime.model_timing import ModelCallTiming
from agent_runtime.token_accounting import empty_session_usage


class SessionUsageMixin:
    """Mixin: ``session_usage``, ``last_call_usage``, and ``last_call_timing(s)``."""

    def _init_usage_tracking(self) -> None:
        self.last_usage: dict = {}
        self.last_call_usage: dict = {}
        self.last_call_timing: ModelCallTiming | None = None
        self.last_call_timings: list[ModelCallTiming] = []
        self.session_usage: dict = empty_session_usage()

    def reset_session_usage(self) -> None:
        """清零本次 session 的 token 与 API 调用计数。"""
        self.last_usage = {}
        self.last_call_usage = {}
        self.last_call_timing = None
        self.last_call_timings = []
        self.session_usage = empty_session_usage()
