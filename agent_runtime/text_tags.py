"""文本标签提取（<tool> / <final> 等）。"""

from __future__ import annotations

__all__ = ["extract_between_tags"]


def extract_between_tags(text: str, open_tag: str, close_tag: str) -> str:
    """从标签之间提取文本（不处理嵌套大括号，由调用方解析 JSON）。"""
    start = text.find(open_tag)
    if start == -1:
        return ""
    start += len(open_tag)
    end = text.find(close_tag, start)
    if end == -1:
        return ""
    return text[start:end].strip()
