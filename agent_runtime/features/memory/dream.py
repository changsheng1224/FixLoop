"""Memory Dream 后台任务（V1.4-Bonus9b）。

idle / repair 结束时执行：去重 · 过期 · index 重建。
"""

from __future__ import annotations

import time
from typing import Any

from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES

_DEFAULT_TTL_DAYS = 30


class MemoryDreamer:
    """记忆维护器：去重、过期、裁剪，返回统计信息。"""

    def __init__(self, memory_state: dict[str, Any]):
        self._state = memory_state
        self.stats: dict[str, int] = {
            "deduped": 0,
            "expired": 0,
            "trimmed": 0,
            "total_before": 0,
            "total_after": 0,
        }

    def run(self, *, ttl_days: int = _DEFAULT_TTL_DAYS) -> dict:
        """执行全部维护步骤，返回统计 dict。

        Args:
            ttl_days: 笔记过期天数。0=不禁用过期，负值=跳过过期。
        """
        notes = self._state.get("episodic_notes", [])
        self.stats["total_before"] = len(notes)

        self._deduplicate()
        if ttl_days >= 0:
            self._expire(ttl_days)
        self._trim()

        self.stats["total_after"] = len(self._state.get("episodic_notes", []))
        return dict(self.stats)

    def _deduplicate(self) -> int:
        """移除重复笔记（相同 text 只保留最新的，按 note_index）。"""
        notes = self._state.get("episodic_notes", [])
        if not notes:
            return 0
        seen: dict[str, int] = {}
        for i, note in enumerate(notes):
            text = note.get("text", "").strip()
            if text:
                seen[text] = i
        kept = [notes[i] for i in sorted(seen.values())]
        removed = len(notes) - len(kept)
        self._state["episodic_notes"] = kept
        self.stats["deduped"] = removed
        return removed

    def _expire(self, ttl_days: int) -> int:
        """移除过期笔记（TTL 超过 ttl_days 天）。"""
        if ttl_days <= 0:
            return 0
        cutoff = time.time() - ttl_days * 86400
        notes = self._state.get("episodic_notes", [])
        kept = [
            n for n in notes
            if n.get("created_at", float("inf")) >= cutoff
        ]
        removed = len(notes) - len(kept)
        self._state["episodic_notes"] = kept
        self.stats["expired"] = removed
        return removed

    def _trim(self) -> int:
        """裁剪超出 MAX_EPISODIC_NOTES 的笔记。"""
        notes = self._state.get("episodic_notes", [])
        if len(notes) <= MAX_EPISODIC_NOTES:
            return 0
        removed = len(notes) - MAX_EPISODIC_NOTES
        self._state["episodic_notes"] = notes[-MAX_EPISODIC_NOTES:]
        self.stats["trimmed"] = removed
        return removed


def run_memory_dream(memory_state: dict, *, ttl_days: int = _DEFAULT_TTL_DAYS) -> dict:
    """便捷函数：对 memory_state 执行 dream 并返回统计信息。"""
    dreamer = MemoryDreamer(memory_state)
    return dreamer.run(ttl_days=ttl_days)


def dream_summary_to_trace(stats: dict) -> dict:
    """将 dream 统计转为 trace 事件 payload。"""
    return {
        "deduped": stats.get("deduped", 0),
        "expired": stats.get("expired", 0),
        "trimmed": stats.get("trimmed", 0),
        "total_before": stats.get("total_before", 0),
        "total_after": stats.get("total_after", 0),
    }
