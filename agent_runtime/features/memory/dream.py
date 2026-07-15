"""Memory Dream 后台任务（V1.4-Bonus9b）。

idle / repair 结束时执行：去重 · 过期 · index 重建。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES
from agent_runtime.features.memory.episodic import PROMOTE_THRESHOLD as _PROMOTE_THRESHOLD

_DEFAULT_TTL_DAYS = 30
MAX_DURABLE_ENTRIES_PER_TOPIC = 20

# 与 episodic.PROMOTE_THRESHOLD 同源（统一配置面）
PROMOTE_SUGGEST_HIT_MIN = _PROMOTE_THRESHOLD  # kind=decision 且 retrieve_count≥N 时建议晋升


class MemoryDreamer:
    """记忆维护器：去重、过期、裁剪、durable GC、晋升建议、路由重建。"""

    def __init__(self, memory_state: dict[str, Any], durable_root: str = ""):
        self._state = memory_state
        self._durable_root = durable_root
        self.stats: dict[str, int] = {
            "deduped": 0,
            "expired": 0,
            "trimmed": 0,
            "durable_gc": 0,
            "total_before": 0,
            "total_after": 0,
            "promotion_suggestions": 0,
            "routing_entries": 0,
        }
        self.promotion_hints: list[dict] = []

    def run(
        self,
        *,
        ttl_days: int = _DEFAULT_TTL_DAYS,
        max_durable: int = MAX_DURABLE_ENTRIES_PER_TOPIC,
    ) -> dict:
        """执行全部维护步骤，返回统计 dict。

        Args:
            ttl_days: 笔记过期天数。0=不禁用过期，负值=跳过过期。
            max_durable: 每个 topic 文件最大条目数。0=不禁用。
        """
        notes = self._state.get("episodic_notes", [])
        self.stats["total_before"] = len(notes)

        self._deduplicate()
        if ttl_days >= 0:
            self._expire(ttl_days)
        self._trim()
        self._suggest_promotions(hit_min=PROMOTE_SUGGEST_HIT_MIN)
        if max_durable > 0:
            self._gc_durable(max_durable)
        self._rebuild_routing_table()

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
        """移除过期笔记（TTL 超限 + 置信度时间衰减过滤）。

        置信度公式: confidence *= DECAY_RATE ** days_since_created
        低于 CONFIDENCE_FLOOR 的笔记视为过期。
        """
        from agent_runtime.features.memory.durable import apply_confidence_decay

        ttl_cutoff = time.time() - ttl_days * 86400 if ttl_days > 0 else 0
        notes = self._state.get("episodic_notes", [])
        kept = []
        decay_expired = 0
        for n in notes:
            created = n.get("created_at", float("inf"))
            # TTL 过期
            if ttl_days > 0 and created < ttl_cutoff:
                continue
            # 置信度时间衰减
            if "confidence" in n:
                decayed, valid = apply_confidence_decay(n["confidence"], created)
                n["confidence"] = decayed
                if not valid:
                    decay_expired += 1
                    continue
            kept.append(n)

        removed = len(notes) - len(kept)
        self._state["episodic_notes"] = kept
        self.stats["expired"] = removed + decay_expired
        return removed

    @staticmethod
    def _sort_by_time(notes: list[dict]) -> list[dict]:
        """按 created_at 升序排列（最旧在前，最新在后）。"""
        return sorted(notes, key=lambda n: n.get("created_at", 0))

    def _trim(self) -> int:
        """裁剪超出 MAX_EPISODIC_NOTES 的笔记（按时间保留最新）。"""
        notes = self._state.get("episodic_notes", [])
        if len(notes) <= MAX_EPISODIC_NOTES:
            return 0
        removed = len(notes) - MAX_EPISODIC_NOTES
        sorted_notes = self._sort_by_time(notes)
        self._state["episodic_notes"] = sorted_notes[-MAX_EPISODIC_NOTES:]
        self.stats["trimmed"] = removed
        return removed

    def _suggest_promotions(self, hit_min: int = PROMOTE_SUGGEST_HIT_MIN) -> int:
        """扫描 episodic notes，kind=decision 且 retrieve_count≥N 生成晋升建议。

        仅生成 suggestion 写入 promotion_hints，默认不自动 promote。
        （自动 promote 由 episodic.retrieval_candidates 在 PROMOTE_THRESHOLD 时触发）
        """
        notes = self._state.get("episodic_notes", [])
        suggestions = []
        for note in notes:
            if note.get("kind") != "decision":
                continue
            rc = note.get("retrieve_count", 0)
            if rc >= hit_min:
                suggestions.append(
                    {
                        "text": note.get("text", "")[:200],
                        "retrieve_count": rc,
                        "kind": note.get("kind"),
                        "source": note.get("source", ""),
                        "note_index": note.get("note_index"),
                    }
                )
        self.promotion_hints = suggestions
        self.stats["promotion_suggestions"] = len(suggestions)
        return len(suggestions)

    def _rebuild_routing_table(self) -> dict[str, int]:
        """重建内存路由表（topic → entries → bytes 概览）。

        当前为轻量实现：扫描 topics_dir 统计条目数。
        完整路由表（inline/chunked strategy）依赖 MEMORY.md 升级。
        """
        if not self._durable_root:
            self.stats["routing_entries"] = 0
            return {}
        topics_dir = Path(self._durable_root) / ".agent" / "memory" / "topics"
        if not topics_dir.is_dir():
            self.stats["routing_entries"] = 0
            return {}

        routing: dict[str, int] = {}
        total = 0
        for md_file in sorted(topics_dir.glob("*.md")):
            try:
                lines = md_file.read_text(encoding="utf-8").splitlines()
                count = sum(1 for line in lines if line.strip() and not line.startswith("#"))
                routing[md_file.stem] = count
                total += count
            except OSError:
                pass
        self.stats["routing_entries"] = total
        return routing

    def _gc_durable(self, max_entries: int) -> int:
        """Durable GC：每个 topic 文件超限时淘汰旧条目。

        按文件 mtime 排序，优先淘汰最旧文件中的条目。
        chunked topic 的 chunk 目录也参与 GC。
        """
        if not self._durable_root:
            return 0
        topics_dir = Path(self._durable_root) / ".agent" / "memory" / "topics"
        if not topics_dir.is_dir():
            return 0

        total_removed = 0
        # 按 mtime 排序文件（旧文件优先处理）
        md_files = sorted(topics_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
        for md_file in md_files:
            try:
                lines = md_file.read_text(encoding="utf-8").splitlines()
                headers = [line for line in lines if line.startswith("#")]
                entries = [line for line in lines if line.strip() and not line.startswith("#")]
                if len(entries) <= max_entries:
                    continue
                removed = len(entries) - max_entries
                # 保留最近 N 条（按追加顺序）
                kept = headers + entries[-max_entries:]
                md_file.write_text("\n".join(kept) + "\n", encoding="utf-8")
                total_removed += removed
            except OSError:
                pass

        # chunked topic GC
        for chunk_dir in sorted(
            topics_dir.glob("*"), key=lambda p: p.stat().st_mtime if p.is_dir() else 0
        ):
            if not chunk_dir.is_dir() or not (chunk_dir / "chunk-0.md").is_file():
                continue
            try:
                total_entries = 0
                for cf in sorted(chunk_dir.glob("chunk-*.md")):
                    lines = cf.read_text(encoding="utf-8").splitlines()
                    total_entries += sum(
                        1 for line in lines if line.strip() and not line.startswith("#")
                    )
                if total_entries <= max_entries:
                    continue
                # 删除最旧 chunk
                chunks = sorted(
                    chunk_dir.glob("chunk-*.md"), key=lambda p: int(p.stem.split("-")[1])
                )
                while total_entries > max_entries and len(chunks) > 1:
                    oldest = chunks.pop(0)
                    lines = oldest.read_text(encoding="utf-8").splitlines()
                    removed = sum(1 for line in lines if line.strip() and not line.startswith("#"))
                    total_entries -= removed
                    oldest.unlink()
                    total_removed += removed
            except OSError:
                pass

        self.stats["durable_gc"] = total_removed
        return total_removed


def run_memory_dream(
    memory_state: dict,
    *,
    ttl_days: int = _DEFAULT_TTL_DAYS,
    durable_root: str = "",
) -> tuple[dict, MemoryDreamer]:
    """便捷函数：对 memory_state 执行 dream 并返回 (统计信息, dreamer)。

    dreamer 含 promotion_hints 列表供调用方写入 trace。
    """
    dreamer = MemoryDreamer(memory_state, durable_root=durable_root)
    return dreamer.run(ttl_days=ttl_days), dreamer


def dream_summary_to_trace(stats: dict, dreamer: MemoryDreamer | None = None) -> dict:
    """将 dream 统计转为 trace 事件 payload。"""
    payload = {
        "deduped": stats.get("deduped", 0),
        "expired": stats.get("expired", 0),
        "trimmed": stats.get("trimmed", 0),
        "durable_gc": stats.get("durable_gc", 0),
        "total_before": stats.get("total_before", 0),
        "total_after": stats.get("total_after", 0),
        "promotion_suggestions": stats.get("promotion_suggestions", 0),
        "routing_entries": stats.get("routing_entries", 0),
    }
    if dreamer is not None and dreamer.promotion_hints:
        payload["promotion_hints"] = dreamer.promotion_hints
    return payload
