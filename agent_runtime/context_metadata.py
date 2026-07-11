"""Context build / trace metadata 辅助。"""

from __future__ import annotations

__all__ = [
    "CONTEXT_TRACE_KEYS",
    "build_trace_payload",
    "merge_template_metadata",
]

CONTEXT_TRACE_KEYS = (
    "context_schema_version",
    "context_sections",
    "context_sections_total",
    "total_tokens",
    "budget",
    "prefix_hashes",
    "task_template_source",
    "task_template_fingerprint",
    "request_preserved",
    "task_budget_overflow",
    "projection_step",
    "sealed_history_count",
    "prefix_aligned",
    "prefix_fingerprint",
)


def merge_template_metadata(metadata: dict, template_meta: dict | None) -> dict:
    """合并 repair/L1 模板观测字段。"""
    if template_meta:
        metadata.update(template_meta)
    return metadata


def build_trace_payload(metadata: dict | None) -> dict:
    """从 ContextManager / fit 元数据提取 context_built trace 字段。"""
    if not metadata:
        return {}
    return {key: metadata.get(key) for key in CONTEXT_TRACE_KEYS if key in metadata}
