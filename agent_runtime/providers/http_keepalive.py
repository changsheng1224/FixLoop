"""HTTP keep-alive 连接复用（V1.4-Bonus12）。

同一 host 的多次 LLM 调用复用底层 HTTPS 连接，
避免每次调用都重建 TCP/TLS 连接。

Usage::

    conn = get_connection("api.deepseek.com", timeout=60)
    conn.request("POST", "/v1/messages", body=..., headers={...})
    response = conn.getresponse()
"""

from __future__ import annotations

import http.client
import ssl
import threading
from urllib.parse import urlparse

# 连接池：{host: HTTPSConnection}
_conn_pool: dict[str, http.client.HTTPSConnection] = {}
_lock = threading.Lock()


def get_connection(url: str, timeout: int = 60) -> http.client.HTTPSConnection:
    """获取或创建指定 host 的复用连接。

    Args:
        url: 完整的请求 URL（如 https://api.deepseek.com/v1/messages）。
        timeout: 连接超时秒数。

    Returns:
        HTTPSConnection 实例。
    """
    host = _extract_host(url)
    key = f"{host}:{timeout}"

    with _lock:
        conn = _conn_pool.get(key)
        if conn is None:
            conn = _create_connection(host, timeout)
            _conn_pool[key] = conn
        return conn


def invalidate_connection(url: str) -> None:
    """连接失效时从池中移除。"""
    host = _extract_host(url)
    with _lock:
        keys = [k for k in _conn_pool if k.startswith(host)]
        for k in keys:
            del _conn_pool[k]


def close_connection(url: str) -> None:
    """关闭指定 host 的缓存连接。"""
    host = _extract_host(url)
    with _lock:
        keys_to_remove = [k for k in _conn_pool if k.startswith(host)]
        for k in keys_to_remove:
            try:
                _conn_pool[k].close()
            except Exception:
                pass
            del _conn_pool[k]
    """关闭指定 host 的缓存连接。"""
    host = _extract_host(url)
    with _lock:
        keys_to_remove = [k for k in _conn_pool if k.startswith(host)]
        for k in keys_to_remove:
            try:
                _conn_pool[k].close()
            except Exception:
                pass
            del _conn_pool[k]


def close_all() -> None:
    """关闭所有缓存连接。"""
    with _lock:
        for conn in _conn_pool.values():
            try:
                conn.close()
            except Exception:
                pass
        _conn_pool.clear()


def _extract_host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or parsed.hostname or "localhost"


def _create_connection(host: str, timeout: int) -> http.client.HTTPSConnection:
    ctx = ssl.create_default_context()
    return http.client.HTTPSConnection(host, timeout=timeout, context=ctx)


def _is_closed(conn: http.client.HTTPSConnection) -> bool:
    """检测连接是否已被关闭（仅在使用后检测；新连接 sock=None 不算 closed）。"""
    # _http_vsn 默认 11；如果为 0 表示连接已被 close() 显式调用过
    # 参考 http.client.HTTPConnection.close() 将 _http_vsn 设为 0
    return getattr(conn, "_http_vsn", 11) == 0
