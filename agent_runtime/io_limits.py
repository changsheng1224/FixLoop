"""工具输出体量与二进制护栏。"""

from __future__ import annotations

import os
from pathlib import Path

# 默认上限（可用环境变量覆盖）
DEFAULT_READ_MAX_BYTES = 512 * 1024  # 512 KiB
DEFAULT_GREP_MAX_BYTES = 256 * 1024
DEFAULT_SHELL_MAX_BYTES = 256 * 1024

_BINARY_MAGIC = (
    b"\x00",
    b"\x7fELF",
    b"MZ",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"PK\x03\x04",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def read_max_bytes() -> int:
    return _env_int("FIXLOOP_READ_MAX_BYTES", DEFAULT_READ_MAX_BYTES)


def grep_max_bytes() -> int:
    return _env_int("FIXLOOP_GREP_MAX_BYTES", DEFAULT_GREP_MAX_BYTES)


def shell_max_bytes() -> int:
    return _env_int("FIXLOOP_SHELL_MAX_BYTES", DEFAULT_SHELL_MAX_BYTES)


def is_likely_binary(path: Path, *, sample_size: int = 8192) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(sample_size)
    except OSError:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    for magic in _BINARY_MAGIC:
        if chunk.startswith(magic):
            return True
    # 高比例不可打印
    nontext = sum(1 for b in chunk if b < 9 or (13 < b < 32) or b == 127)
    return (nontext / max(len(chunk), 1)) > 0.30


def truncate_text(text: str, max_bytes: int, *, label: str = "output") -> tuple[str, bool]:
    """按 UTF-8 字节截断；返回 (text, truncated)。"""
    raw = (text or "").encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text or "", False
    cut = raw[:max_bytes].decode("utf-8", errors="ignore")
    return (
        cut
        + f"\n... [{label} truncated at {max_bytes} bytes; "
        "raise FIXLOOP_*_MAX_BYTES to see more]",
        True,
    )
