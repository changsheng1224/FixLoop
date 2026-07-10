"""Retry-After + jitter 策略单测。"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from agent_runtime.providers.retry_policy import (
    RateLimitExceededError,
    apply_equal_jitter,
    apply_full_jitter,
    compute_rate_limit_delay,
    compute_server_error_delay,
    parse_retry_after,
)


class TestParseRetryAfter:
    def test_integer_seconds(self):
        assert parse_retry_after({"Retry-After": "30"}) == 30.0

    def test_missing_header(self):
        assert parse_retry_after({}) is None
        assert parse_retry_after(None) is None

    def test_http_date_future(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=45)
        headers = {"Retry-After": format_datetime(future, usegmt=True)}
        delay = parse_retry_after(headers)
        assert delay is not None
        assert 40 <= delay <= 50

    def test_invalid_value(self):
        assert parse_retry_after({"Retry-After": "not-a-date"}) is None


class TestJitter:
    def test_equal_jitter_range(self, monkeypatch):
        monkeypatch.setattr(
            "agent_runtime.providers.retry_policy.random.uniform",
            lambda a, b: (a + b) / 2,
        )
        assert apply_equal_jitter(60.0) == 45.0

    def test_equal_jitter_zero(self):
        assert apply_equal_jitter(0.0) == 0.0

    def test_full_jitter_range(self, monkeypatch):
        monkeypatch.setattr(
            "agent_runtime.providers.retry_policy.random.uniform",
            lambda a, b: b,
        )
        assert apply_full_jitter(8.0) == 8.0


class TestComputeRateLimitDelay:
    def test_uses_retry_after_with_equal_jitter(self, monkeypatch):
        monkeypatch.setattr(
            "agent_runtime.providers.retry_policy.random.uniform",
            lambda a, b: a,
        )
        delay = compute_rate_limit_delay(0, 60.0)
        assert delay == 30.0

    def test_exponential_when_no_retry_after(self, monkeypatch):
        monkeypatch.setattr(
            "agent_runtime.providers.retry_policy.random.uniform",
            lambda a, b: a,
        )
        assert compute_rate_limit_delay(2, None, base=1.0) == 2.0

    def test_caps_delay(self, monkeypatch):
        monkeypatch.setattr(
            "agent_runtime.providers.retry_policy.random.uniform",
            lambda a, b: a,
        )
        assert compute_rate_limit_delay(0, 500.0, cap=120.0) == 60.0


class TestComputeServerErrorDelay:
    def test_exponential_full_jitter(self, monkeypatch):
        monkeypatch.setattr(
            "agent_runtime.providers.retry_policy.random.uniform",
            lambda a, b: b,
        )
        assert compute_server_error_delay(1, base=2.0, cap=120.0) == 4.0


class TestRateLimitExceededError:
    def test_is_runtime_error(self):
        err = RateLimitExceededError("429 exhausted")
        assert isinstance(err, RuntimeError)
