"""多轮 messages 前缀对齐：run 级冻结 + history 单调封印。"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any

PROJECTION_STATE_KEY = "_run_projection"


@dataclass
class RunProjectionState:
    """单次 Agent run 的投影状态（存于 session[PROJECTION_STATE_KEY]）。"""

    memory_snapshot: dict
    user_query: str
    sealed_history_count: int = 0
    sealed_history_text: str = ""
    projection_step: int = 0
    last_context_prefix: str = ""


def _get_state(session: dict) -> RunProjectionState | None:
    raw = session.get(PROJECTION_STATE_KEY)
    return raw if isinstance(raw, RunProjectionState) else None


def _require_state(session: dict) -> RunProjectionState:
    state = _get_state(session)
    if state is None:
        raise KeyError("run projection state not initialized; call init_run_projection first")
    return state


def init_run_projection(session: dict, user_query: str) -> None:
    """Run 开始时冻结 memory 快照与检索 query。"""
    session[PROJECTION_STATE_KEY] = RunProjectionState(
        memory_snapshot=copy.deepcopy(session.get("memory", {})),
        user_query=user_query,
    )


def get_sealed_history(session: dict) -> tuple[int, str]:
    """返回 (已封印条目数, 已封印 history 段文本)。"""
    state = _get_state(session)
    if state is None:
        return 0, ""
    return state.sealed_history_count, state.sealed_history_text


def seal_history_at_build(session: dict, history_len: int, history_text: str) -> None:
    """build 完成后封印当前 history 段，供下一步单调追加。"""
    state = _get_state(session)
    if state is None:
        return
    state.sealed_history_count = history_len
    state.sealed_history_text = history_text


def check_prefix_monotonic(previous_prefix: str, current_prefix: str) -> bool:
    """当前前缀必须以历史前缀开头（禁止 divergent）。"""
    if not previous_prefix:
        return True
    return current_prefix.startswith(previous_prefix)


# Backward-compatible alias
check_prefix_aligned = check_prefix_monotonic


def fingerprint_prefix(text: str) -> str:
    """稳定前缀指纹（SHA256）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_context_prefix(_agent: Any, metadata: dict) -> str:
    """从 build 元数据读取可比对前缀（不含 request）。"""
    return str(metadata.get("_context_prefix_text", "") or "")


def attach_projection_metadata(
    metadata: dict,
    session: dict,
    *,
    context_prefix: str = "",
) -> None:
    """写入 projection trace 字段并更新 session 前缀状态。"""
    state = _get_state(session)
    previous = state.last_context_prefix if state is not None else ""
    prefix = context_prefix or previous
    monotonic = check_prefix_monotonic(previous, prefix)
    step = (state.projection_step + 1) if state is not None else 1
    if state is not None:
        state.projection_step = step
        state.last_context_prefix = prefix

    metadata["projection_step"] = step
    metadata["sealed_history_count"] = (
        state.sealed_history_count if state is not None else 0
    )
    metadata["prefix_monotonic"] = monotonic
    metadata["prefix_aligned"] = monotonic
    metadata["prefix_fingerprint"] = fingerprint_prefix(prefix)


def run_memory_snapshot(session: dict) -> dict | None:
    """若存在 run 级 memory 快照则返回，否则 None。"""
    state = _get_state(session)
    if state is None:
        return None
    return state.memory_snapshot


def run_user_query(session: dict, fallback: str = "") -> str:
    """run 级固定检索 query。"""
    state = _get_state(session)
    if state is not None and state.user_query:
        return state.user_query
    return fallback
