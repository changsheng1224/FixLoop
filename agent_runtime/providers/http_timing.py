"""HTTP response body read helpers with TTFB timing."""

from __future__ import annotations

import time


def read_http_body_with_timing(resp, chunk_size: int = 8192) -> tuple[bytes, int, int]:
    """Read an HTTP response body in chunks; return (body, ttft_ms, total_ms).

    *ttft_ms* is time from first ``read`` to first non-empty chunk (TTFB).
    """
    t0 = time.time()
    t_first: float | None = None
    chunks: list[bytes] = []
    while True:
        chunk = resp.read(chunk_size)
        if not chunk:
            break
        if t_first is None:
            t_first = time.time()
        chunks.append(chunk)
    t_end = time.time()
    if t_first is None:
        t_first = t_end
    ttft_ms = int((t_first - t0) * 1000)
    total_ms = int((t_end - t0) * 1000)
    return b"".join(chunks), ttft_ms, total_ms
