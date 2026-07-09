"""prefix 稳定段：禁动态字段校验与 cache hash。"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "PrefixStableError",
    "FORBIDDEN_STABLE_PREFIX_PATTERNS",
    "assert_stable_prefix_clean",
    "hash_stable_prefix",
]

FORBIDDEN_STABLE_PREFIX_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\btimestamp\b", "timestamp"),
    (r"\brun_id\b", "run_id"),
    (r"\bsession_id\b", "session_id"),
    (r"\bnonce\b", "nonce"),
    (r"\buuid\b", "uuid"),
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "ISO datetime"),
)


class PrefixStableError(ValueError):
    """稳定 prefix 段含禁止的动态字段。"""


def assert_stable_prefix_clean(stable_text: str) -> None:
    """校验 persona/rules/tools/examples 或 L2 system 段不含动态字段。"""
    for pattern, label in FORBIDDEN_STABLE_PREFIX_PATTERNS:
        if re.search(pattern, stable_text, re.IGNORECASE):
            raise PrefixStableError(
                f"稳定 prefix 段禁止包含动态字段 ({label}); "
                "请将 run_id/session_id/timestamp 等放入 history 或 trace。"
            )


def hash_stable_prefix(stable_text: str) -> str:
    """稳定段 SHA256，用作 prompt_cache_key。"""
    return hashlib.sha256(stable_text.encode()).hexdigest()
