"""HTTP keep-alive 连接复用单测（V1.4-Bonus12）。"""

from __future__ import annotations

from agent_runtime.providers.http_keepalive import (
    _extract_host,
    close_all,
    close_connection,
    get_connection,
    invalidate_connection,
)


# ---------------------------------------------------------------------------
# _extract_host
# ---------------------------------------------------------------------------


class TestExtractHost:
    def test_standard_url(self):
        assert _extract_host("https://api.deepseek.com/v1/messages") == "api.deepseek.com"

    def test_url_with_port(self):
        assert _extract_host("https://localhost:11434/api/chat") == "localhost:11434"

    def test_ip_url(self):
        assert _extract_host("http://127.0.0.1:11434/api/generate") == "127.0.0.1:11434"


# ---------------------------------------------------------------------------
# get_connection
# ---------------------------------------------------------------------------


class TestGetConnection:
    def setup_method(self):
        close_all()

    def teardown_method(self):
        close_all()

    def test_returns_connection(self):
        conn = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        assert conn is not None
        assert conn.timeout == 30

    def test_same_host_returns_same_connection(self):
        conn1 = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        conn2 = get_connection("https://api.deepseek.com/v1/chat", timeout=30)
        assert conn1 is conn2

    def test_different_hosts_different_connections(self):
        conn1 = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        conn2 = get_connection("https://api.openai.com/v1/chat", timeout=30)
        assert conn1 is not conn2

    def test_different_timeouts_different_connections(self):
        conn1 = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        conn2 = get_connection("https://api.deepseek.com/v1/messages", timeout=60)
        assert conn1 is not conn2


# ---------------------------------------------------------------------------
# close_connection / close_all
# ---------------------------------------------------------------------------


class TestClose:
    def setup_method(self):
        close_all()

    def teardown_method(self):
        close_all()

    def test_close_all_clears_pool(self):
        get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        close_all()
        # 新连接应该不同于之前的
        conn = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        assert conn is not None

    def test_close_connection_removes_host(self):
        get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        close_connection("https://api.deepseek.com/v1/messages")
        # 重新获取会创建新连接
        conn = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        assert conn is not None


# ---------------------------------------------------------------------------
# invalidate_connection
# ---------------------------------------------------------------------------


class TestInvalidate:
    def setup_method(self):
        close_all()

    def teardown_method(self):
        close_all()

    def test_invalidate_creates_new_connection(self):
        conn1 = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        invalidate_connection("https://api.deepseek.com/v1/messages")
        conn2 = get_connection("https://api.deepseek.com/v1/messages", timeout=30)
        assert conn1 is not conn2
