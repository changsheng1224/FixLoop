"""Langfuse exporter：Canonical Trace → ingestion batch（Fake client）。"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from agent_runtime.metrics import _reset_registry_for_tests
from agent_runtime.observability.langfuse_exporter import (
    FakeLangfuseClient,
    LangfuseExporter,
    export_canonical_record,
    langfuse_enabled,
    record_to_ingestion_events,
    reset_exporter_for_tests,
)
from agent_runtime.run_store import RunStore

SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "examples" / "canonical-trace-sample.jsonl"


@pytest.fixture(autouse=True)
def _reset():
    _reset_registry_for_tests()
    reset_exporter_for_tests()
    yield
    _reset_registry_for_tests()
    reset_exporter_for_tests()


def _sample_records() -> list[dict]:
    rows = []
    for line in SAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class TestRecordMapping:
    def test_first_event_emits_trace_and_span(self):
        rec = _sample_records()[0]
        items = record_to_ingestion_events(rec, emit_trace=True)
        types = [i["type"] for i in items]
        assert "trace-create" in types
        assert "span-create" in types
        trace = next(i for i in items if i["type"] == "trace-create")
        assert trace["body"]["id"] == rec["trace_id"]

    def test_model_event_is_generation(self):
        rec = {
            "schema_version": "1",
            "run_id": "r1",
            "trace_id": "r1",
            "span_id": "s1",
            "parent_span_id": None,
            "event": "model_complete",
            "event_type": "model_complete",
            "timestamp": "2026-08-04T00:00:00+00:00",
            "created_at": "2026-08-04T00:00:00+00:00",
            "status": "ok",
            "seq": 3,
            "payload": {"model": "gpt-test", "usage": {"input": 1, "output": 2}},
        }
        items = record_to_ingestion_events(rec, emit_trace=False)
        assert len(items) == 1
        assert items[0]["type"] == "generation-create"
        assert items[0]["body"]["model"] == "gpt-test"

    def test_sample_jsonl_full_export(self):
        client = FakeLangfuseClient()
        exporter = LangfuseExporter(client=client)
        for rec in _sample_records():
            exporter.export_record(rec)
        assert client.calls == len(_sample_records())
        types = [e["type"] for e in client.all_events]
        assert types.count("trace-create") == 1
        assert "span-create" in types
        # 可查看的完整轨迹：state + tool 事件均在
        names = [e["body"]["name"] for e in client.all_events if e["type"] != "trace-create"]
        assert "repair_started" in names
        assert "tool_executed" in names
        assert "repair_finished" in names


class TestFailSoftAndRedaction:
    def test_exporter_failure_does_not_raise(self):
        client = FakeLangfuseClient()
        client.fail_next = True
        export_canonical_record(
            {
                "event": "repair_started",
                "trace_id": "t-fail",
                "run_id": "t-fail",
                "timestamp": "2026-08-04T00:00:00+00:00",
                "status": "ok",
                "seq": 1,
            },
            client=client,
        )
        # 第二次应成功（fail_next 已消费）
        export_canonical_record(
            {
                "event": "repair_finished",
                "trace_id": "t-fail",
                "run_id": "t-fail",
                "timestamp": "2026-08-04T00:00:01+00:00",
                "status": "ok",
                "seq": 2,
            },
            client=client,
        )
        assert client.calls >= 1

    def test_run_store_keeps_jsonl_when_exporter_fails(self, monkeypatch):
        client = FakeLangfuseClient()
        client.fail_next = True
        monkeypatch.setenv("FIXLOOP_LANGFUSE_ENABLED", "1")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        reset_exporter_for_tests()
        from agent_runtime.observability.langfuse_exporter import get_exporter

        get_exporter().set_client(client)

        root = tempfile.mkdtemp(prefix="fixloop-langfuse-")
        try:
            store = RunStore(root)
            store.append_trace_event(
                "run-x", "repair_started", {"api_key": "sk-secret-should-redact"}
            )
            events = store.load_trace_events("run-x")
            assert len(events) == 1
            assert events[0]["event"] == "repair_started"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_payload_redacted_in_langfuse_body(self):
        rec = {
            "run_id": "r",
            "trace_id": "r",
            "event": "tool_executed",
            "timestamp": "2026-08-04T00:00:00+00:00",
            "status": "unset",
            "seq": 1,
            "payload": {"api_key": "sk-live-abcdef", "tool": "read_file"},
        }
        items = record_to_ingestion_events(rec, emit_trace=False)
        body = items[0]["body"]
        meta_payload = body.get("metadata", {}).get("payload") or {}
        # 敏感 key 应被脱敏
        assert meta_payload.get("api_key") == "<redacted>" or "sk-live" not in json.dumps(
            body
        )

    def test_disabled_without_keys(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        monkeypatch.delenv("FIXLOOP_LANGFUSE_ENABLED", raising=False)
        assert langfuse_enabled() is False
