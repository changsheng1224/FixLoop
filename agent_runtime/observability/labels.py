"""Prometheus Label 低基数保护。

计划约束：Label 仅允许模型、阶段、Skill、状态、版本等低基数字段；
禁止 ``run_id`` / ``user_id`` / ``issue_id``（及同类高基数字段）。
"""

from __future__ import annotations

import os
import re
from typing import Any

# 绝对禁止出现在 Prometheus Label 中（高基数 / PII）
FORBIDDEN_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "run_id",
        "user_id",
        "issue_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "task_id",
        "session_id",
        "path",
        "repo",
        "repo_url",
        "pr_url",
        "commit",
        "sha",
    }
)

# 周计划明确允许的低基数字段 + 既有 runtime/intent 枚举字段
ALLOWED_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "phase",
        "skill",
        "status",
        "version",
        "tier",
        "event_category",
        # Intent Router（既有）
        "channel",
        "mode",
        "primary",
        "action",
        "parser",
        "reason",
        "outcome",
        "slot",
        "bucket",
    }
)

_SAFE_VALUE_RE = re.compile(r"[^a-zA-Z0-9_.:\-/=+@]")


def metrics_version() -> str:
    """``FIXLOOP_METRICS_VERSION``，默认 ``1``。"""
    return os.environ.get("FIXLOOP_METRICS_VERSION", "1").strip() or "1"


def sanitize_label_value(value: Any, *, max_len: int = 64) -> str:
    """压缩 Label 值：去空白、剔非法字符、截断。"""
    text = str(value if value is not None else "unknown").strip() or "unknown"
    text = _SAFE_VALUE_RE.sub("_", text)
    if len(text) > max_len:
        text = text[:max_len]
    return text or "unknown"


def strip_forbidden_labels(labels: dict[str, str] | None) -> dict[str, str] | None:
    """剔除禁止键；保留其余（兼容既有 Intent Label）。"""
    if not labels:
        return labels
    out = {k: v for k, v in labels.items() if k not in FORBIDDEN_LABEL_KEYS}
    return out or None


def low_cardinality_labels(**fields: Any) -> dict[str, str]:
    """只保留白名单键，并规范化值。始终附带 ``version``。"""
    out: dict[str, str] = {"version": metrics_version()}
    for key, value in fields.items():
        if key in FORBIDDEN_LABEL_KEYS:
            continue
        if key not in ALLOWED_LABEL_KEYS:
            continue
        if value is None or value == "":
            continue
        out[key] = sanitize_label_value(value)
    return out


def assert_no_forbidden_labels(labels: dict[str, str] | None) -> None:
    """测试辅助：发现禁止键则抛 AssertionError。"""
    if not labels:
        return
    bad = sorted(k for k in labels if k in FORBIDDEN_LABEL_KEYS)
    if bad:
        raise AssertionError(f"forbidden prometheus labels: {bad}")
