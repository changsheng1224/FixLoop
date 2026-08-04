"""可观测导出：Canonical Trace → Prometheus / Langfuse（fail-soft）。"""

from __future__ import annotations

from typing import Any

__all__ = [
    "after_trace_append",
    "export_canonical_record",
    "low_cardinality_labels",
    "record_canonical_event",
    "strip_forbidden_labels",
]


def after_trace_append(record: dict[str, Any]) -> None:
    """``RunStore.append_trace_event`` 写盘成功后的统一钩子。

    Prometheus 与 Langfuse 任一失败均不影响主路径。
    """
    try:
        from agent_runtime.observability.prom_from_trace import record_canonical_event

        record_canonical_event(record)
    except Exception:
        pass
    try:
        from agent_runtime.observability.langfuse_exporter import export_canonical_record

        export_canonical_record(record)
    except Exception:
        pass


def export_canonical_record(record: dict[str, Any], **kwargs: Any) -> None:
    from agent_runtime.observability.langfuse_exporter import (
        export_canonical_record as _export,
    )

    _export(record, **kwargs)


def record_canonical_event(record: dict[str, Any], **kwargs: Any) -> None:
    from agent_runtime.observability.prom_from_trace import (
        record_canonical_event as _rec,
    )

    _rec(record, **kwargs)


def low_cardinality_labels(**fields: Any) -> dict[str, str]:
    from agent_runtime.observability.labels import low_cardinality_labels as _lc

    return _lc(**fields)


def strip_forbidden_labels(labels: dict[str, str] | None) -> dict[str, str] | None:
    from agent_runtime.observability.labels import strip_forbidden_labels as _sf

    return _sf(labels)
