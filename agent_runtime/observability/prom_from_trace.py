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

SLO_THRESHOLDS_MS = {
    "repair": 300_000,
    "tool": 30_000,
    "evaluation": 600_000,
}


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

        duration = payload.get("duration_ms") or payload.get("elapsed_ms")
        if duration is not None:
            duration_ms = float(duration)
            if category == "tool":
                registry.histogram_observe(
                    "fixloop_tool_duration_ms",
                    duration_ms,
                    labels=low_cardinality_labels(phase=_phase_label(payload)),
                )
            elif category == "evaluation":
                registry.histogram_observe(
                    "fixloop_eval_duration_ms", duration_ms
                )
            elif event in {"repair_finished", "run_finished"}:
                registry.histogram_observe("fixloop_repair_duration_ms", duration_ms)
            slo_kind = "evaluation" if category == "evaluation" else (
                "tool" if category == "tool" else "repair"
            )
            if duration_ms > SLO_THRESHOLDS_MS[slo_kind]:
                registry.counter_inc(
                    "fixloop_slo_exceeded_total",
                    labels=low_cardinality_labels(operation=slo_kind),
                )

        registry.counter_inc(
            "fixloop_trace_events_total",
            labels=low_cardinality_labels(
                event_category=category,
                status=status,
            ),
        )

        if event == "observation_stored":
            registry.counter_inc(
                "fixloop_observation_events_total",
                labels=low_cardinality_labels(
                    tool=sanitize_label_value(payload.get("tool", "unknown")),
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

        if event.startswith("skill_"):
            registry.counter_inc(
                "fixloop_skill_events_total",
                labels=low_cardinality_labels(
                    skill=_skill_label(payload),
                    status=status,
                    phase=event.removeprefix("skill_"),
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

        if event == "budget_exhausted":
            registry.counter_inc(
                "fixloop_budget_exhausted_total",
                labels=low_cardinality_labels(
                    resource=sanitize_label_value(payload.get("resource", "unknown")),
                    action=sanitize_label_value(payload.get("action", "stop")),
                ),
            )
        if event == "latency_slo_exceeded":
            registry.counter_inc(
                "fixloop_latency_slo_exceeded_total",
                labels=low_cardinality_labels(
                    kind=sanitize_label_value(payload.get("kind", "unknown")),
                ),
            )
        if event == "latency_degraded":
            for action in payload.get("actions", []) or []:
                registry.counter_inc(
                    "fixloop_degradation_total",
                    labels=low_cardinality_labels(action=sanitize_label_value(action)),
                )

        if event in {"security_denied", "sandbox_violation"}:
            registry.counter_inc(
                "fixloop_security_denials_total",
                labels=low_cardinality_labels(
                    reason=sanitize_label_value(payload.get("reason", event))
                ),
            )
        if event == "patch_rollback":
            registry.counter_inc(
                "fixloop_patch_rollbacks_total",
                labels=low_cardinality_labels(
                    reason=sanitize_label_value(payload.get("reason", "unknown"))
                ),
            )
        if event == "stale_patch_rejected":
            registry.counter_inc(
                "fixloop_stale_patch_rejections_total",
                labels=low_cardinality_labels(
                    reason=sanitize_label_value(payload.get("reason", "base_hash_mismatch"))
                ),
            )
        if event == "sandbox_policy":
            registry.counter_inc(
                "fixloop_sandbox_policy_events_total",
                labels=low_cardinality_labels(
                    action=sanitize_label_value(payload.get("action", "evaluate")),
                    reason=sanitize_label_value(payload.get("policy", "preferred")),
                ),
            )
        if event in {"worktree_created", "worktree_removed", "worktree_lease"}:
            registry.counter_inc(
                "fixloop_worktree_events_total",
                labels=low_cardinality_labels(
                    action=sanitize_label_value(payload.get("action", event))
                ),
            )
        if event == "workspace_policy":
            registry.counter_inc(
                "fixloop_workspace_policy_events_total",
                labels=low_cardinality_labels(
                    action=sanitize_label_value(payload.get("action", "evaluate"))
                ),
            )
    except Exception:
        pass
