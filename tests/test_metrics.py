"""Prometheus MetricsRegistry 单测（含 HTTP 端点）。"""

import threading
import time
import urllib.request
from http.server import HTTPServer

import pytest

from agent_runtime.metrics import (
    MetricsRegistry,
    _MetricsHandler,
    _reset_registry_for_tests,
    get_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


class TestMetricsRegistry:
    """注册表 CRUD 测试。"""

    def test_counter_inc_basic(self):
        reg = MetricsRegistry()
        reg.counter_inc("test_counter")
        reg.counter_inc("test_counter", 2)
        rendered = reg.render()
        assert "test_counter 3" in rendered

    def test_counter_with_labels(self):
        reg = MetricsRegistry()
        reg.counter_inc("test_labeled", labels={"tier": "host"})
        reg.counter_inc("test_labeled", labels={"tier": "container"})
        reg.counter_inc("test_labeled", labels={"tier": "host"})
        rendered = reg.render()
        assert 'test_labeled{tier="host"} 2' in rendered
        assert 'test_labeled{tier="container"} 1' in rendered

    def test_gauge_set(self):
        reg = MetricsRegistry()
        reg.gauge_set("test_gauge", 3.14)
        rendered = reg.render()
        assert "test_gauge 3.14" in rendered

    def test_gauge_inc(self):
        reg = MetricsRegistry()
        reg.gauge_inc("test_gauge", 1.5)
        reg.gauge_inc("test_gauge", 2.5)
        rendered = reg.render()
        assert "test_gauge 4" in rendered

    def test_render_includes_help_and_type(self):
        reg = MetricsRegistry()
        reg.counter_inc("fixloop_tool_steps_total", labels={"tier": "host"})
        rendered = reg.render()
        assert "# HELP fixloop_tool_steps_total" in rendered
        assert "# TYPE fixloop_tool_steps_total counter" in rendered

    def test_reset_clears_all(self):
        reg = MetricsRegistry()
        reg.counter_inc("test_counter", 5)
        reg.gauge_set("test_gauge", 1.0)
        reg.reset()
        rendered = reg.render()
        assert "test_counter" not in rendered
        assert "test_gauge" not in rendered

    def test_global_registry_is_singleton(self):
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_thread_safety(self):
        reg = MetricsRegistry()
        errors = []

        def inc_many():
            try:
                for _ in range(100):
                    reg.counter_inc("thread_test", labels={"t": "a"})
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=inc_many) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        rendered = reg.render()
        assert 'thread_test{t="a"} 1000' in rendered


class TestMetricsHttpEndpoint:
    """HTTP /metrics 端点测试。"""

    @pytest.fixture
    def server_port(self):
        """在随机端口启动 daemon metrics server。"""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        server = HTTPServer(("127.0.0.1", port), _MetricsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.1)
        yield port
        server.shutdown()

    def test_metrics_endpoint_returns_prometheus_text(self, server_port):
        get_registry().counter_inc("fixloop_tool_steps_total", labels={"tier": "host"})
        url = f"http://127.0.0.1:{server_port}/metrics"
        with urllib.request.urlopen(url) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
        assert "fixloop_tool_steps_total" in body

    def test_health_endpoint(self, server_port):
        url = f"http://127.0.0.1:{server_port}/health"
        with urllib.request.urlopen(url) as resp:
            assert resp.status == 200

    def test_reset_endpoint(self, server_port):
        get_registry().counter_inc("test_counter", 5)
        url = f"http://127.0.0.1:{server_port}/reset"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
        assert "test_counter" not in get_registry().render()
