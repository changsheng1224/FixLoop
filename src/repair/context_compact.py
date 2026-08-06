"""CC 序工具史压缩 + compact_thrash 停损。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["CompactResult", "compact_tool_history", "estimate_chars"]


def estimate_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages or [])


@dataclass
class CompactResult:
    messages: list[dict[str, Any]]
    compacted: bool = False
    thrash: bool = False
    compact_count: int = 0
    compact_thrash_count: int = 0
    dropped_chars: int = 0


def _is_failure_content(text: str) -> bool:
    t = text or ""
    low = t.lower()
    return (
        t.startswith("Error")
        or "fail" in low
        or "near=" in low
        or "reject" in low
        or "traceback" in low
        or "assertionerror" in low
    )


def compact_tool_history(
    messages: list[dict[str, Any]] | None,
    *,
    keep_failed: int = 3,
    max_success_chars: int = 400,
    max_total_chars: int = 24_000,
    thrash_threshold: int = 3,
    prior_compacts: int = 0,
) -> CompactResult:
    """丢旧成功 tool 大输出；保留失败/种子；检测 thrash。"""
    msgs = [dict(m) for m in (messages or [])]
    before = estimate_chars(msgs)
    if before <= max_total_chars and prior_compacts == 0:
        # 仍可做轻量截断成功块
        pass

    if prior_compacts >= thrash_threshold and before > max_total_chars:
        return CompactResult(
            messages=msgs,
            compacted=False,
            thrash=True,
            compact_count=prior_compacts,
            compact_thrash_count=1,
        )

    failed_idxs: list[int] = []
    for i, m in enumerate(msgs):
        if _is_failure_content(str(m.get("content") or "")):
            failed_idxs.append(i)
    keep_fail = set(failed_idxs[-keep_failed:])

    out: list[dict[str, Any]] = []
    changed = False
    for i, m in enumerate(msgs):
        content = str(m.get("content") or "")
        role = m.get("role")
        # 保留短消息、失败、非 user 工具结果以外的结构
        if i in keep_fail or role == "system":
            out.append(m)
            continue
        if role == "user" and len(content) > max_success_chars and not _is_failure_content(
            content
        ):
            # 疑似成功 tool 大输出 → 摘要
            head = content[: max_success_chars // 2]
            tail = content[-80:] if len(content) > 80 else ""
            new_c = f"{head}\n...[compacted {len(content)} chars]...\n{tail}"
            nm = dict(m)
            nm["content"] = new_c
            out.append(nm)
            changed = True
            continue
        out.append(m)

    after = estimate_chars(out)
    compact_count = prior_compacts + (1 if changed or after < before else 0)
    thrash = False
    thrash_n = 0
    if compact_count >= thrash_threshold and after > max_total_chars:
        thrash = True
        thrash_n = 1

    return CompactResult(
        messages=out,
        compacted=changed or after < before,
        thrash=thrash,
        compact_count=compact_count,
        compact_thrash_count=thrash_n,
        dropped_chars=max(0, before - after),
    )
