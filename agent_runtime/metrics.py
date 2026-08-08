"""零依赖 Prometheus 指标注册表 + HTTP /metrics 端点。

使用方式：
    from agent_runtime.metrics import get_registry
    registry = get_registry()
    registry.counter_inc("fixloop_tool_steps_total", labels={"tier": "host"})

    # 启动 HTTP 端点（daemon 线程）
    from agent_runtime.metrics import start_metrics_server
    start_metrics_server(port=9090)
"""

from __future__ import annotations

import http.server
import os
import threading


def _default_port() -> int:
    try:
        return int(os.environ.get("FIXLOOP_METRICS_PORT", "9090"))
    except ValueError:
        return 9090


# Prometheus 指标描述（HELP 行）
_METRIC_HELP: dict[str, str] = {
    "fixloop_tool_steps_total": "Total tool executions by tier.",
    "fixloop_repair_phase_ms": "Repair phase wall-clock duration in milliseconds.",
    "fixloop_repair_status": "Repair outcome count by status.",
    "fixloop_token_usage_total": "Total token consumption across all repairs.",
    "fixloop_cache_hit_rate": "Prompt cache hit rate (0.0–1.0).",
    "fixloop_retry_count": "Current repair retry count.",
    "fixloop_repair_duration_ms": "Repair duration distribution in milliseconds.",
    "fixloop_tool_duration_ms": "Tool duration distribution in milliseconds.",
    "fixloop_eval_duration_ms": "Evaluation duration distribution in milliseconds.",
    "fixloop_slo_exceeded_total": "SLO violations by operation.",
    # Canonical Trace → Prometheus（低基数）
    "fixloop_trace_events_total": "Canonical Trace events by category and status.",
    "fixloop_skill_matched_total": "Skill match outcomes by skill and status.",
    "fixloop_errors_total": "Error/cancel events by phase and status.",
    "fixloop_model_events_total": "Model lifecycle events by model/phase/status.",
    "fixloop_budget_exhausted_total": "Budget exhaustion events by resource/action.",
    "fixloop_latency_slo_exceeded_total": "Latency SLO violations by kind.",
    "fixloop_degradation_total": "Adaptive degradation decisions by action.",
    "fixloop_observation_events_total": "Canonical Observation Store events by tool/status.",
    "fixloop_security_denials_total": "Security policy denials by reason.",
    "fixloop_patch_rollbacks_total": "Patch transaction rollbacks by reason.",
    "fixloop_sandbox_policy_events_total": "Sandbox policy decisions by policy/action.",
    "fixloop_worktree_events_total": "Worktree lifecycle and lease events.",
    "fixloop_stale_patch_rejections_total": "Compare-and-swap stale patch rejections.",
    "fixloop_workspace_policy_events_total": "Workspace policy events by action.",
    # Intent Router (online)
    "fixloop_intent_routed_total": (
        "Intent router invocations by channel/mode/primary/action/parser."
    ),
    "fixloop_intent_misroute_proxy_total": (
        "Online misroute proxies (low_conf/conflict/clarify/llm_override)."
    ),
    "fixloop_intent_clarify_total": "Clarify outcomes by channel and reason.",
    "fixloop_intent_llm_fallback_total": "Graph-level LLM fallback attempts by outcome.",
    "fixloop_intent_confidence": "Last intent confidence by channel/mode.",
    "fixloop_intent_confidence_bucket_total": "Intent confidence distribution buckets.",
    "fixloop_intent_latency_ms": "Last intent route latency in milliseconds.",
    "fixloop_intent_latency_bucket_total": "Intent route latency distribution buckets.",
    "fixloop_intent_exec_nodes": "Executable node count of last routed graph.",
    "fixloop_intent_slot_filled_total": "Slot fill events from routed intents.",
    "fixloop_intent_embed_skip_total": "Embedding layer skipped.",
    "fixloop_intent_conflict_total": "Rule vs embed conflicts observed.",
    "fixloop_intent_action_total": "Downstream action counts.",
}

_METRIC_TYPE: dict[str, str] = {
    "fixloop_tool_steps_total": "counter",
    "fixloop_repair_phase_ms": "gauge",
    "fixloop_repair_status": "counter",
    "fixloop_token_usage_total": "counter",
    "fixloop_cache_hit_rate": "gauge",
    "fixloop_retry_count": "gauge",
    "fixloop_repair_duration_ms": "histogram",
    "fixloop_tool_duration_ms": "histogram",
    "fixloop_eval_duration_ms": "histogram",
    "fixloop_slo_exceeded_total": "counter",
    "fixloop_trace_events_total": "counter",
    "fixloop_skill_matched_total": "counter",
    "fixloop_errors_total": "counter",
    "fixloop_model_events_total": "counter",
    "fixloop_budget_exhausted_total": "counter",
    "fixloop_latency_slo_exceeded_total": "counter",
    "fixloop_degradation_total": "counter",
    "fixloop_observation_events_total": "counter",
    "fixloop_security_denials_total": "counter",
    "fixloop_patch_rollbacks_total": "counter",
    "fixloop_sandbox_policy_events_total": "counter",
    "fixloop_worktree_events_total": "counter",
    "fixloop_stale_patch_rejections_total": "counter",
    "fixloop_workspace_policy_events_total": "counter",
    "fixloop_intent_routed_total": "counter",
    "fixloop_intent_misroute_proxy_total": "counter",
    "fixloop_intent_clarify_total": "counter",
    "fixloop_intent_llm_fallback_total": "counter",
    "fixloop_intent_confidence": "gauge",
    "fixloop_intent_confidence_bucket_total": "counter",
    "fixloop_intent_latency_ms": "gauge",
    "fixloop_intent_latency_bucket_total": "counter",
    "fixloop_intent_exec_nodes": "gauge",
    "fixloop_intent_slot_filled_total": "counter",
    "fixloop_intent_embed_skip_total": "counter",
    "fixloop_intent_conflict_total": "counter",
    "fixloop_intent_action_total": "counter",
}


def _format_labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def _render_metric_group(
    name: str, labeled: dict[tuple, int | float], value_fmt: str = "{}"
) -> list[str]:
    """渲染单个 metric 的 HELP + TYPE + 值行。"""
    lines: list[str] = []
    help_text = _METRIC_HELP.get(name, "")
    type_text = _METRIC_TYPE.get(name, "untyped")
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {type_text}")
    for label_tuple, value in sorted(labeled.items()):
        labels = dict(pair.split("=", 1) for pair in label_tuple) if label_tuple else {}
        lbl = _format_labels(labels)
        lines.append(f"{name}{lbl} {value_fmt.format(value)}")
    return lines


def _render_prometheus(
    counters: dict[str, dict[tuple, int]],
    gauges: dict[str, dict[tuple, float]],
    histograms: dict[str, dict[tuple, list[float]]],
) -> str:
    """渲染 Prometheus text exposition format。"""
    lines: list[str] = []
    for name, labeled in sorted(counters.items()):
        lines.extend(_render_metric_group(name, labeled))
    for name, labeled in sorted(gauges.items()):
        lines.extend(_render_metric_group(name, labeled, value_fmt="{:.6g}"))
    for name, labeled in sorted(histograms.items()):
        help_text = _METRIC_HELP.get(name, "")
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} histogram")
        for label_tuple, values in sorted(labeled.items()):
            labels = dict(pair.split("=", 1) for pair in label_tuple) if label_tuple else {}
            suffix = _format_labels(labels)
            lines.append(f"{name}_count{suffix} {len(values)}")
            lines.append(f"{name}_sum{suffix} {sum(values):.6g}")
    lines.append("")
    return "\n".join(lines)


def _label_key(labels: dict[str, str] | None) -> tuple[str, ...]:
    if not labels:
        return ()
    return tuple(f"{k}={v}" for k, v in sorted(labels.items()))


def _safe_labels(labels: dict[str, str] | None) -> dict[str, str] | None:
    """剔除高基数禁止键（run_id/user_id/issue_id 等）。"""
    try:
        from agent_runtime.observability.labels import strip_forbidden_labels

        return strip_forbidden_labels(labels)
    except Exception:
        if not labels:
            return labels
        forbidden = {"run_id", "user_id", "issue_id"}
        return {k: v for k, v in labels.items() if k not in forbidden} or None


class MetricsRegistry:
    """线程安全的 Prometheus 指标注册表。

    支持 counter（只增）和 gauge（可增可减）两种类型。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, int]] = {}
        self._gauges: dict[str, dict[tuple, float]] = {}
        self._histograms: dict[str, dict[tuple, list[float]]] = {}

    # ── counter ──

    def counter_inc(self, name: str, value: int = 1, labels: dict[str, str] | None = None):
        labels = _safe_labels(labels)
        with self._lock:
            labeled = self._counters.setdefault(name, {})
            key = _label_key(labels)
            labeled[key] = labeled.get(key, 0) + value

    # ── gauge ──

    def gauge_set(self, name: str, value: float, labels: dict[str, str] | None = None):
        labels = _safe_labels(labels)
        with self._lock:
            labeled = self._gauges.setdefault(name, {})
            key = _label_key(labels)
            labeled[key] = float(value)

    def gauge_inc(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None):
        labels = _safe_labels(labels)
        with self._lock:
            labeled = self._gauges.setdefault(name, {})
            key = _label_key(labels)
            labeled[key] = labeled.get(key, 0.0) + float(value)

    def histogram_observe(
        self, name: str, value: float, labels: dict[str, str] | None = None
    ) -> None:
        labels = _safe_labels(labels)
        with self._lock:
            labeled = self._histograms.setdefault(name, {})
            key = _label_key(labels)
            samples = labeled.setdefault(key, [])
            samples.append(float(value))
            if len(samples) > 10000:
                del samples[:-10000]

    # ── render ──

    def render(self) -> str:
        with self._lock:
            return _render_prometheus(
                dict(self._counters),
                dict(self._gauges),
                dict(self._histograms),
            )

    # ── reset ──

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# 进程级单例
_registry: MetricsRegistry | None = None


def get_registry() -> MetricsRegistry:
    global _registry
    if _registry is None:
        _registry = MetricsRegistry()
    return _registry


def _reset_registry_for_tests() -> None:
    """测试辅助：重置全局单例。"""
    global _registry
    _registry = None


# ── HTTP 端点 ──


class _MetricsHandler(http.server.BaseHTTPRequestHandler):
    """GET /metrics → Prometheus 文本；POST /reset → 清零。"""

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志

    def do_GET(self):  # noqa: N802 - http.server protocol method
        if self.path == "/metrics":
            body = get_registry().render()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802 - http.server protocol method
        if self.path == "/reset":
            get_registry().reset()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"reset"}\n')
        else:
            self.send_response(404)
            self.end_headers()


def start_metrics_server(port: int | None = None) -> threading.Thread:
    """启动 HTTP /metrics daemon 线程。

    Args:
        port: 监听端口（默认 ``FIXLOOP_METRICS_PORT`` 或 9090）。

    Returns:
        已启动的 daemon 线程。
    """
    if port is None:
        port = _default_port()

    server = http.server.HTTPServer(("127.0.0.1", port), _MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
