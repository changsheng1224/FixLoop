"""Online Prometheus recording for IntentRouter (no ground-truth labels)."""

from __future__ import annotations

from agent_runtime.intent.models import IntentResult, RouteContext
from agent_runtime.metrics import get_registry

# Confidence buckets for distribution without histogram type.
_CONF_BUCKETS = (
    ("0_0.45", 0.0, 0.45),
    ("0.45_0.60", 0.45, 0.60),
    ("0.60_1.0", 0.60, 1.01),
)

_LAT_BUCKETS = (
    ("0_5", 0.0, 5.0),
    ("5_20", 5.0, 20.0),
    ("20_100", 20.0, 100.0),
    ("100_inf", 100.0, 1e12),
)


def _conf_bucket(conf: float) -> str:
    for name, lo, hi in _CONF_BUCKETS:
        if lo <= conf < hi:
            return name
    return _CONF_BUCKETS[-1][0]


def _lat_bucket(ms: float) -> str:
    for name, lo, hi in _LAT_BUCKETS:
        if lo <= ms < hi:
            return name
    return _LAT_BUCKETS[-1][0]


def record_intent_route(
    result: IntentResult,
    ctx: RouteContext,
    *,
    latency_ms: float = 0.0,
    embed_skipped: bool = False,
    llm_outcome: str | None = None,
) -> None:
    """Record online metrics after a successful route() call."""
    reg = get_registry()
    channel = ctx.channel
    mode = result.graph.mode if result.graph else "single"
    primary = result.primary or "unknown"
    action = result.action or "unknown"
    parser = (result.parser or "rule").replace("{", "").replace("}", "")[:48]

    reg.counter_inc(
        "fixloop_intent_routed_total",
        labels={
            "channel": channel,
            "mode": mode,
            "primary": primary,
            "action": action,
            "parser": parser,
        },
    )
    reg.counter_inc(
        "fixloop_intent_action_total",
        labels={"channel": channel, "action": action},
    )
    reg.gauge_set(
        "fixloop_intent_confidence",
        result.confidence,
        labels={"channel": channel, "mode": mode},
    )
    reg.counter_inc(
        "fixloop_intent_confidence_bucket_total",
        labels={"channel": channel, "bucket": _conf_bucket(result.confidence)},
    )
    reg.gauge_set(
        "fixloop_intent_latency_ms",
        float(latency_ms),
        labels={"channel": channel},
    )
    reg.counter_inc(
        "fixloop_intent_latency_bucket_total",
        labels={"channel": channel, "bucket": _lat_bucket(float(latency_ms))},
    )

    execs = [n for n in result.graph.nodes if n.role == "executable"] if result.graph else []
    reg.gauge_set(
        "fixloop_intent_exec_nodes",
        float(len(execs)),
        labels={"channel": channel, "mode": mode},
    )

    for slot_name, val in (result.slots or {}).items():
        if slot_name.startswith("_"):
            continue
        if val in (None, "", [], {}):
            continue
        reg.counter_inc(
            "fixloop_intent_slot_filled_total",
            labels={"channel": channel, "slot": str(slot_name)[:32]},
        )

    signals = result.raw_signals or {}
    reg.counter_inc(
        "fixloop_intent_router_version_total",
        labels={
            "router_version": str(signals.get("router_version") or "legacy")[:32],
            "taxonomy_version": str(signals.get("taxonomy_version") or "legacy")[:32],
        },
    )
    for stage, value in (signals.get("stage_latency_ms") or {}).items():
        reg.gauge_set(
            "fixloop_intent_stage_latency_ms",
            float(value or 0.0),
            labels={"channel": channel, "stage": str(stage)[:24]},
        )

    risk_decision = signals.get("risk_decision") or {}
    if risk_decision:
        reg.counter_inc(
            "fixloop_intent_risk_decision_total",
            labels={
                "channel": channel,
                "risk": str(risk_decision.get("risk") or "unknown")[:16],
                "decision": "execute" if risk_decision.get("allow_execute") else "clarify",
            },
        )
    llm_runtime = signals.get("llm_runtime") or {}
    fallback_reason = str(llm_runtime.get("fallback_reason") or "")
    if fallback_reason:
        reg.counter_inc(
            "fixloop_intent_llm_degraded_total",
            labels={"channel": channel, "reason": fallback_reason[:32]},
        )
    if primary == "clarify" or action == "clarify":
        from agent_runtime.intent.clarify import normalize_clarify_reason

        reason = normalize_clarify_reason(
            str(signals.get("clarify_reason") or result.reason or "ambiguous")
        )
        reg.counter_inc(
            "fixloop_intent_clarify_total",
            labels={"channel": channel, "reason": reason},
        )
        reg.counter_inc(
            "fixloop_intent_misroute_proxy_total",
            labels={"channel": channel, "reason": reason},
        )

    if result.confidence < ctx.tau_clarify:
        reg.counter_inc(
            "fixloop_intent_misroute_proxy_total",
            labels={"channel": channel, "reason": "low_conf"},
        )
    elif result.confidence < ctx.tau_exec and primary not in ("help", "cancel", "clarify"):
        reg.counter_inc(
            "fixloop_intent_misroute_proxy_total",
            labels={"channel": channel, "reason": "below_tau_exec"},
        )

    conflict = False
    for n in result.graph.nodes if result.graph else []:
        if "_embed_conflict" in (n.slots or {}):
            conflict = True
            break
    if conflict or signals.get("conflict"):
        reg.counter_inc(
            "fixloop_intent_conflict_total",
            labels={"channel": channel},
        )
        reg.counter_inc(
            "fixloop_intent_misroute_proxy_total",
            labels={"channel": channel, "reason": "conflict"},
        )

    if embed_skipped:
        reg.counter_inc(
            "fixloop_intent_embed_skip_total",
            labels={"channel": channel, "reason": "unavailable"},
        )

    if llm_outcome:
        reg.counter_inc(
            "fixloop_intent_llm_fallback_total",
            labels={"channel": channel, "outcome": llm_outcome},
        )
        if llm_outcome == "applied":
            reg.counter_inc(
                "fixloop_intent_misroute_proxy_total",
                labels={"channel": channel, "reason": "llm_override"},
            )

    anaphora = signals.get("anaphora") or {}
    outcome = anaphora.get("outcome")
    if outcome and outcome != "passthrough":
        reg.counter_inc(
            "fixloop_intent_anaphora_total",
            labels={"channel": channel, "outcome": str(outcome)[:32]},
        )

    for key in signals.get("candidate_keys") or []:
        reg.counter_inc(
            "fixloop_intent_candidate_total",
            labels={"channel": channel, "key": str(key)[:48]},
        )
    for ev in signals.get("candidate_events") or []:
        if isinstance(ev, dict) and ev.get("source"):
            reg.counter_inc(
                "fixloop_intent_candidate_source_total",
                labels={"channel": channel, "source": str(ev["source"])[:32]},
            )

    from agent_runtime.intent.slo import evaluate_route_slo

    for violation in evaluate_route_slo(
        latency_ms=latency_ms,
        risk_decision=risk_decision,
        llm_runtime=llm_runtime,
        embed_skipped=embed_skipped,
        action=result.action,
    ):
        reg.counter_inc(
            "fixloop_intent_slo_violation_total",
            labels={"channel": channel, "kind": violation},
        )


def record_feedback_write(*, status: str, strength: str = "weak") -> None:
    get_registry().counter_inc(
        "fixloop_intent_feedback_write_total",
        labels={"status": str(status)[:16], "strength": str(strength)[:16]},
    )
