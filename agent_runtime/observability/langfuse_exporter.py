"""Canonical Trace → Langfuse Ingestion API 适配层（零 SDK，fail-soft）。

默认关闭；配置 ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` 后自动启用
（可用 ``FIXLOOP_LANGFUSE_ENABLED=0`` 强制关闭）。

导出失败不影响主任务；payload 经 ``redact_artifact`` 脱敏。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Protocol

from agent_runtime.canonical_trace import EVENT_CATALOG
from agent_runtime.observability.prom_from_trace import event_category

_DEFAULT_HOST = "https://cloud.langfuse.com"


class LangfuseClient(Protocol):
    def ingest(self, batch: list[dict[str, Any]]) -> None: ...


class FakeLangfuseClient:
    """测试用：记录所有 batch，不发网络。"""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.fail_next = False
        self.calls = 0

    def ingest(self, batch: list[dict[str, Any]]) -> None:
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("fake langfuse unavailable")
        self.batches.append(list(batch))

    @property
    def all_events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for b in self.batches:
            out.extend(b)
        return out


class HttpLangfuseClient:
    """POST ``/api/public/ingestion``（Basic auth）。"""

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str = _DEFAULT_HOST,
        timeout_sec: float = 5.0,
    ) -> None:
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host.rstrip("/")
        self.timeout_sec = timeout_sec

    def ingest(self, batch: list[dict[str, Any]]) -> None:
        url = f"{self.host}/api/public/ingestion"
        body = json.dumps({"batch": batch}, ensure_ascii=False, default=str).encode("utf-8")
        token = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                "User-Agent": "FixLoop-LangfuseExporter/1",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            resp.read()


def langfuse_enabled() -> bool:
    flag = os.environ.get("FIXLOOP_LANGFUSE_ENABLED", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def _timeout_sec() -> float:
    try:
        return float(os.environ.get("FIXLOOP_LANGFUSE_TIMEOUT_SEC", "5"))
    except ValueError:
        return 5.0


def build_http_client_from_env() -> HttpLangfuseClient | None:
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not pub or not sec:
        return None
    host = os.environ.get("LANGFUSE_HOST", _DEFAULT_HOST).strip() or _DEFAULT_HOST
    return HttpLangfuseClient(
        public_key=pub,
        secret_key=sec,
        host=host,
        timeout_sec=_timeout_sec(),
    )


def _redact_payload(payload: Any) -> Any:
    try:
        from agent_runtime.security import redact_artifact

        return redact_artifact(payload)
    except Exception:
        return None


def _obs_type(event: str) -> str:
    cat = event_category(event)
    if cat == "model" or event.startswith("model_"):
        return "generation-create"
    return "span-create"


def _level_for_status(status: str) -> str:
    if status == "error":
        return "ERROR"
    if status == "cancelled":
        return "WARNING"
    return "DEFAULT"


def record_to_ingestion_events(
    record: dict[str, Any],
    *,
    emit_trace: bool,
) -> list[dict[str, Any]]:
    """Canonical 信封 → Langfuse ingestion batch items。"""
    event = str(record.get("event") or record.get("event_type") or "")
    if not event:
        return []
    trace_id = str(record.get("trace_id") or record.get("run_id") or "")
    if not trace_id:
        return []
    ts = str(record.get("timestamp") or record.get("created_at") or "")
    seq = record.get("seq")
    obs_id = f"{trace_id}:{seq}" if seq is not None else f"{trace_id}:{uuid.uuid4().hex[:12]}"
    status = str(record.get("status") or "unset")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    safe_payload = _redact_payload(payload) if payload else None
    parent = record.get("parent_span_id")
    category = event_category(event)

    items: list[dict[str, Any]] = []
    if emit_trace:
        items.append(
            {
                "id": str(uuid.uuid4()),
                "timestamp": ts,
                "type": "trace-create",
                "body": {
                    "id": trace_id,
                    "timestamp": ts,
                    "name": "fixloop.repair",
                    "metadata": {
                        "schema_version": record.get("schema_version"),
                        "source": "canonical_trace",
                    },
                    "tags": ["fixloop", "canonical-trace"],
                },
            }
        )

    body: dict[str, Any] = {
        "id": obs_id,
        "traceId": trace_id,
        "name": event,
        "startTime": ts,
        "endTime": ts,
        "parentObservationId": parent,
        "metadata": {
            "event_category": category,
            "status": status,
            "span_id": record.get("span_id"),
            "seq": seq,
        },
        "level": _level_for_status(status),
    }
    if safe_payload is not None:
        # 不把高基数标识当 userId；仅放脱敏 metadata / input
        body["metadata"]["payload"] = safe_payload
        body["input"] = safe_payload

    if _obs_type(event) == "generation-create":
        model = None
        if isinstance(safe_payload, dict):
            model = safe_payload.get("model") or safe_payload.get("model_name")
        if model:
            body["model"] = str(model)
        usage = None
        if isinstance(safe_payload, dict):
            usage = safe_payload.get("usage") or safe_payload.get("token_usage")
        if isinstance(usage, dict):
            body["usage"] = usage
        items.append(
            {
                "id": str(uuid.uuid4()),
                "timestamp": ts,
                "type": "generation-create",
                "body": body,
            }
        )
    else:
        items.append(
            {
                "id": str(uuid.uuid4()),
                "timestamp": ts,
                "type": "span-create",
                "body": body,
            }
        )
    return items


class LangfuseExporter:
    """进程级导出器：按 trace_id 首次事件发 trace-create。"""

    def __init__(self, client: LangfuseClient | None = None) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._seen_traces: set[str] = set()
        self._max_seen = 10_000

    def set_client(self, client: LangfuseClient | None) -> None:
        self._client = client

    def reset_for_tests(self) -> None:
        with self._lock:
            self._seen_traces.clear()
            self._client = None

    def _resolve_client(self) -> LangfuseClient | None:
        if self._client is not None:
            return self._client
        if not langfuse_enabled():
            return None
        return build_http_client_from_env()

    def export_record(self, record: dict[str, Any]) -> None:
        """导出单条记录；失败静默。"""
        try:
            client = self._resolve_client()
            if client is None:
                return
            trace_id = str(record.get("trace_id") or record.get("run_id") or "")
            if not trace_id:
                return
            with self._lock:
                emit_trace = trace_id not in self._seen_traces
                if emit_trace:
                    if len(self._seen_traces) >= self._max_seen:
                        self._seen_traces.clear()
                    self._seen_traces.add(trace_id)
            batch = record_to_ingestion_events(record, emit_trace=emit_trace)
            if not batch:
                return
            client.ingest(batch)
        except Exception:
            pass


_exporter: LangfuseExporter | None = None
_exporter_lock = threading.Lock()


def get_exporter() -> LangfuseExporter:
    global _exporter
    with _exporter_lock:
        if _exporter is None:
            _exporter = LangfuseExporter()
        return _exporter


def reset_exporter_for_tests() -> None:
    get_exporter().reset_for_tests()


def export_canonical_record(record: dict[str, Any], client: LangfuseClient | None = None) -> None:
    """模块级入口（fail-soft）。"""
    try:
        exporter = get_exporter()
        if client is not None:
            exporter.set_client(client)
        exporter.export_record(record)
    except Exception:
        pass


# 抑制 unused 警告：文档化目录引用
_ = EVENT_CATALOG
