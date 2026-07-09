"""模型可见的工具 schema 视图（不含 run 实现指针）。"""

from __future__ import annotations

__all__ = ["TOOL_SPEC_PUBLIC_KEYS", "tool_schema_view"]

TOOL_SPEC_PUBLIC_KEYS = frozenset({"schema", "description", "risky"})


def tool_schema_view(registry: dict) -> dict[str, dict]:
    """返回供 prompt / native API 使用的工具描述（仅 schema 相关字段）。"""
    view: dict[str, dict] = {}
    for name, spec in registry.items():
        view[name] = {key: spec[key] for key in TOOL_SPEC_PUBLIC_KEYS if key in spec}
    return view
