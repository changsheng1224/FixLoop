"""从 Canonical Trace 事件更新低基数 Prometheus 指标（fail-soft）。

与 Langfuse 共用同一事件源，保证 Metrics / Trace 口径一致。
``run_id`` / ``user_id`` / ``issue_id`` 永不进入 Label。
"""

from __future__ import annotations

from typing import Any

from agent_runtime.canonical_trace import EVENT_CATALOG
from agent_runtime.observability.labels import low_cardinality_labels, sanitize_label_value

# 反向：event → category
_EVENT_TO_CATEGORY: dict[str, str] = {}
for _cat, _names in EVENT_CATALOG.items():
    for _name in _names:
        _EVENT_TO_CATEGORY[_name] = _cat


def event_category(event: str) -> str:
    return _EVENT_TO_CATEGORY.get(event, "other")


def _skill_label(payload: dict[str, Any]) -> str:
    for key in ("skill", "skill_id", "skill_name", "matched_skill"):
        val = payload.get(key)
        if isinstance(val, dict):
            val = val.get("id") or val.get("name") or val.get("skill_id")
        if val:
            return sanitize_label_value(val)
    strategy = payload.get("fallback_strategy")
    if strategy:
        return sanitize_label_value(f"miss:{strategy}")
    return "unknown"


def _phase_label(payload: dict[str, Any]) -> str:
    for key in ("l2_phase", "phase", "agent"):
        val = payload.get(key)
        if val:
            return sanitize_label_value(val)
    return "unknown"


def record_canonical_event(record: dict[str, Any], registry: Any | None = None) -> None:
    """根据一条 Canonical 记录更新指标；任何异常静默吞掉。"""
    try:
        if registry is None:
            from agent_runtime.metrics import get_registry

            registry = get_registry()
        event = str(record.get("event") or record.get("event_type") or "")
        if not event:
            return
        status = str(record.get("status") or "unset")
        category = event_category(event)
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}

        registry.counter_inc(
            "fixloop_trace_events_total",
            labels=low_cardinality_labels(
                event_category=category,
                status=status,
            ),
        )

        if event == "skill_matched":
            registry.counter_inc(
                "fixloop_skill_matched_total",
                labels=low_cardinality_labels(
                    skill=_skill_label(payload),
                    status=status,
                ),
            )

        if status == "error" or event in ("repair_cancelled", "run_cancelled"):
            registry.counter_inc(
                "fixloop_errors_total",
                labels=low_cardinality_labels(
                    phase=_phase_label(payload),
                    status="error" if status == "error" else "cancelled",
                ),
            )

        if event in ("model_complete", "model_request_start"):
            model = payload.get("model") or payload.get("model_name") or "unknown"
            registry.counter_inc(
                "fixloop_model_events_total",
                labels=low_cardinality_labels(
                    model=model,
                    status=status,
                    phase=_phase_label(payload),
                ),
            )
    except Exception:
        pass
