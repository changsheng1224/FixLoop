"""Prometheus 高基数 Label 保护与 Trace→Metrics 口径。"""

from __future__ import annotations

import shutil
import tempfile

import pytest

from agent_runtime.metrics import MetricsRegistry, _reset_registry_for_tests, get_registry
from agent_runtime.observability.labels import (
    FORBIDDEN_LABEL_KEYS,
    assert_no_forbidden_labels,
    low_cardinality_labels,
    strip_forbidden_labels,
)
from agent_runtime.observability.prom_from_trace import record_canonical_event
from agent_runtime.run_store import RunStore


@pytest.fixture(autouse=True)
def _reset_registry():
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


class TestLabelGuard:
    def test_strip_forbidden(self):
        labels = {
            "status": "ok",
            "run_id": "r-123",
            "user_id": "u-1",
            "issue_id": "ISSUE-9",
            "phase": "localize",
        }
        cleaned = strip_forbidden_labels(labels)
        assert cleaned is not None
        assert "run_id" not in cleaned
        assert "user_id" not in cleaned
        assert "issue_id" not in cleaned
        assert cleaned["status"] == "ok"
        assert cleaned["phase"] == "localize"

    def test_low_cardinality_drops_unknown_and_forbidden(self):
        labels = low_cardinality_labels(
            status="fixed",
            phase="verify",
            skill="python-fix",
            run_id="MUST_NOT_APPEAR",
            issue_id="MUST_NOT",
            weird_high_card="path/to/file.py",
        )
        assert_no_forbidden_labels(labels)
        assert "run_id" not in labels
        assert "issue_id" not in labels
        assert "weird_high_card" not in labels
        assert labels["status"] == "fixed"
        assert labels["skill"] == "python-fix"
        assert "version" in labels

    def test_registry_strips_forbidden_on_inc(self):
        reg = MetricsRegistry()
        reg.counter_inc(
            "fixloop_repair_status",
            labels={"status": "fixed", "run_id": "r1", "user_id": "u1"},
        )
        rendered = reg.render()
        assert 'status="fixed"' in rendered
        assert "run_id" not in rendered
        assert "user_id" not in rendered
        for key in FORBIDDEN_LABEL_KEYS:
            assert f"{key}=" not in rendered


class TestPromFromTrace:
    def test_trace_events_and_skill(self):
        reg = get_registry()
        record_canonical_event(
            {
                "event": "skill_matched",
                "status": "ok",
                "payload": {"skill_id": "stack-localize"},
            },
            registry=reg,
        )
        text = reg.render()
        assert "fixloop_trace_events_total" in text
        assert 'event_category="skill"' in text
        assert "fixloop_skill_matched_total" in text
        assert 'skill="stack-localize"' in text
        assert "run_id" not in text

    def test_error_increments_errors_total(self):
        reg = get_registry()
        record_canonical_event(
            {
                "event": "span_closed",
                "status": "error",
                "payload": {"l2_phase": "patch", "reason": "abnormal"},
            },
            registry=reg,
        )
        text = reg.render()
        assert "fixloop_errors_total" in text
        assert 'phase="patch"' in text

    def test_run_store_hook_updates_metrics(self):
        root = tempfile.mkdtemp(prefix="fixloop-prom-")
        try:
            store = RunStore(root)
            store.append_trace_event(
                "run-prom",
                "tool_executed",
                {"tool": "read_file", "run_id": "should-not-label"},
            )
            text = get_registry().render()
            assert "fixloop_trace_events_total" in text
            assert 'event_category="tool"' in text
            assert 'run_id="run-prom"' not in text
        finally:
            shutil.rmtree(root, ignore_errors=True)
