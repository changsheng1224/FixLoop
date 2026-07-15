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
}

_METRIC_TYPE: dict[str, str] = {
    "fixloop_tool_steps_total": "counter",
    "fixloop_repair_phase_ms": "gauge",
    "fixloop_repair_status": "counter",
    "fixloop_token_usage_total": "counter",
    "fixloop_cache_hit_rate": "gauge",
    "fixloop_retry_count": "gauge",
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
) -> str:
    """渲染 Prometheus text exposition format。"""
    lines: list[str] = []
    for name, labeled in sorted(counters.items()):
        lines.extend(_render_metric_group(name, labeled))
    for name, labeled in sorted(gauges.items()):
        lines.extend(_render_metric_group(name, labeled, value_fmt="{:.6g}"))
    lines.append("")
    return "\n".join(lines)


def _label_key(labels: dict[str, str] | None) -> tuple[str, ...]:
    if not labels:
        return ()
    return tuple(f"{k}={v}" for k, v in sorted(labels.items()))


class MetricsRegistry:
    """线程安全的 Prometheus 指标注册表。

    支持 counter（只增）和 gauge（可增可减）两种类型。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, int]] = {}
        self._gauges: dict[str, dict[tuple, float]] = {}

    # ── counter ──

    def counter_inc(self, name: str, value: int = 1, labels: dict[str, str] | None = None):
        with self._lock:
            labeled = self._counters.setdefault(name, {})
            key = _label_key(labels)
            labeled[key] = labeled.get(key, 0) + value

    # ── gauge ──

    def gauge_set(self, name: str, value: float, labels: dict[str, str] | None = None):
        with self._lock:
            labeled = self._gauges.setdefault(name, {})
            key = _label_key(labels)
            labeled[key] = float(value)

    def gauge_inc(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None):
        with self._lock:
            labeled = self._gauges.setdefault(name, {})
            key = _label_key(labels)
            labeled[key] = labeled.get(key, 0.0) + float(value)

    # ── render ──

    def render(self) -> str:
        with self._lock:
            return _render_prometheus(
                dict(self._counters),
                dict(self._gauges),
            )

    # ── reset ──

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


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
