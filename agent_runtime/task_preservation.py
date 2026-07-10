"""Task / user message 保护：reserve-first 与 preserve 断言。"""

from __future__ import annotations

__all__ = [
    "issue_preserved",
    "reserve_section_budget",
    "task_preservation_metadata",
]


def reserve_section_budget(total_limit: int, request_tokens: int) -> int:
    """为 task/request 预留后，其余 section 可用 token 上限。"""
    return max(0, total_limit - request_tokens)


def task_preservation_metadata(
    request_tokens: int,
    total_limit: int,
) -> dict[str, bool]:
    """写入 request 保护相关 metadata 标志。"""
    meta: dict[str, bool] = {"request_preserved": True}
    if request_tokens > total_limit:
        meta["task_budget_overflow"] = True
    return meta


def issue_preserved(original: str, rendered: str) -> bool:
    """canonical issue/task 文本是否完整出现在渲染结果中。"""
    if not original:
        return True
    return original in (rendered or "")
