"""多轮 messages 前缀对齐：run 级冻结 + history 单调封印。"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

RUN_MEMORY_SNAPSHOT_KEY = "_run_memory_snapshot"
RUN_USER_QUERY_KEY = "_run_user_query"
SEALED_HISTORY_COUNT_KEY = "_sealed_history_count"
SEALED_HISTORY_TEXT_KEY = "_sealed_history_text"
PROJECTION_STEP_KEY = "_projection_step"
LAST_CONTEXT_PREFIX_KEY = "_last_context_prefix"


def init_run_projection(session: dict, user_query: str) -> None:
    """Run 开始时冻结 memory 快照与检索 query。"""
    session[RUN_MEMORY_SNAPSHOT_KEY] = copy.deepcopy(session.get("memory", {}))
    session[RUN_USER_QUERY_KEY] = user_query
    session[SEALED_HISTORY_COUNT_KEY] = 0
    session[SEALED_HISTORY_TEXT_KEY] = ""
    session[PROJECTION_STEP_KEY] = 0
    session[LAST_CONTEXT_PREFIX_KEY] = ""


def get_sealed_history(session: dict) -> tuple[int, str]:
    """返回 (已封印条目数, 已封印 history 段文本)。"""
    count = int(session.get(SEALED_HISTORY_COUNT_KEY, 0) or 0)
    text = str(session.get(SEALED_HISTORY_TEXT_KEY, "") or "")
    return count, text


def seal_history_at_build(session: dict, history_len: int, history_text: str) -> None:
    """build 完成后封印当前 history 段，供下一步单调追加。"""
    session[SEALED_HISTORY_COUNT_KEY] = history_len
    session[SEALED_HISTORY_TEXT_KEY] = history_text


def check_prefix_aligned(previous_prefix: str, current_prefix: str) -> bool:
    """当前前缀必须以历史前缀开头（禁止 divergent）。"""
    if not previous_prefix:
        return True
    return current_prefix.startswith(previous_prefix)


def fingerprint_prefix(text: str) -> str:
    """稳定前缀指纹（SHA256）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_context_prefix(agent: Any, metadata: dict) -> str:
    """从 build 元数据重建可比对前缀（不含 request）。"""
    from agent_runtime.context_manager import ContextManager

    cm = ContextManager(agent)
    query = run_user_query(cm.agent.session, "")
    history_text = str(metadata.get("_history_section_text", "") or "")
    parts = [
        cm._get_system(),
        cm._get_tools(),
        cm._get_skills(),
        cm._get_workspace(),
        cm._get_memory(),
        cm._get_relevant(query),
        history_text,
    ]
    return "\n".join(part for part in parts if part)


def attach_projection_metadata(
    metadata: dict,
    session: dict,
    *,
    context_prefix: str = "",
) -> None:
    """写入 projection trace 字段并更新 session 前缀状态。"""
    previous = str(session.get(LAST_CONTEXT_PREFIX_KEY, "") or "")
    prefix = context_prefix or previous
    aligned = check_prefix_aligned(previous, prefix)
    step = int(session.get(PROJECTION_STEP_KEY, 0) or 0) + 1
    session[PROJECTION_STEP_KEY] = step
    session[LAST_CONTEXT_PREFIX_KEY] = prefix

    metadata["projection_step"] = step
    metadata["sealed_history_count"] = int(session.get(SEALED_HISTORY_COUNT_KEY, 0) or 0)
    metadata["prefix_aligned"] = aligned
    metadata["prefix_fingerprint"] = fingerprint_prefix(prefix)


def run_memory_snapshot(session: dict) -> dict | None:
    """若存在 run 级 memory 快照则返回，否则 None。"""
    snap = session.get(RUN_MEMORY_SNAPSHOT_KEY)
    return snap if isinstance(snap, dict) else None


def run_user_query(session: dict, fallback: str = "") -> str:
    """run 级固定检索 query。"""
    frozen = session.get(RUN_USER_QUERY_KEY)
    if frozen:
        return str(frozen)
    return fallback
