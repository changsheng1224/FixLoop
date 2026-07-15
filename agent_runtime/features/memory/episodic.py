"""事件记忆 — Episodic Memory：会话级工具执行笔记。"""

import time
from typing import TYPE_CHECKING

from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES

if TYPE_CHECKING:
    from agent_runtime.features.memory.durable import DurableMemoryStore

# kind=decision 笔记被检索 >= PROMOTE_THRESHOLD 次时触发晋升逻辑
PROMOTE_THRESHOLD = 3
# 是否自动写入 durable（默认关闭，仅累计 retrieve_count + Dream 生成 hints）
AUTO_PROMOTE = False
# kind 分类权重：error > decision > observation
KIND_WEIGHTS = {"error": 2.0, "decision": 1.5, "observation": 1.0}


def append_note(
    state: dict,
    text: str,
    tags: list[str] | None = None,
    source: str = "",
    kind: str = "observation",
):
    """向 episodic_notes 追加一条工具执行笔记（去重 + 上限裁剪）。"""
    if tags is None:
        tags = []
    notes = state["episodic_notes"]
    if notes and notes[-1].get("text") == text:
        return  # dedupe
    note_index = state["next_note_index"]
    state["next_note_index"] = note_index + 1
    note = {
        "text": text[:300],
        "tags": tags[:5],
        "source": source,
        "created_at": time.time(),
        "note_index": note_index,
        "kind": kind,
        "retrieve_count": 0,
    }
    notes.append(note)
    if len(notes) > MAX_EPISODIC_NOTES:
        # 按时间保留最新 N 条
        sorted_notes = sorted(notes, key=lambda n: n.get("created_at", 0))
        state["episodic_notes"] = sorted_notes[-MAX_EPISODIC_NOTES:]


def retrieval_candidates(
    state: dict,
    query: str,
    limit: int = 3,
    *,
    durable_store: "DurableMemoryStore | None" = None,
) -> list[dict]:
    """按关键词/tag 对 episodic notes 打分排序，返回 top-k。

    kind=decision 笔记每次命中累加 retrieve_count。
    仅当 AUTO_PROMOTE=True 时，达到 PROMOTE_THRESHOLD 才自动写入 durable。
    默认 AUTO_PROMOTE=False：仅累计计数，由 Dream._suggest_promotions 生成 hints。
    """
    query_lower = query.lower()
    query_tokens = set(query_lower.split())
    notes = state.get("episodic_notes", [])
    if not notes:
        return []
    scored = []
    now = time.time()
    promotions: list[tuple[str, str]] = []
    for note in notes:
        score = 0.0
        text = note.get("text", "").lower()
        tags = [t.lower() for t in note.get("tags", [])]
        for token in query_tokens:
            if token in tags:
                score += 3.0
            if token in text:
                score += 1.0
        for tag in tags:
            if tag in query_lower:
                score += 2.0
        if score > 0:
            score *= KIND_WEIGHTS.get(note.get("kind", "observation"), 1.0)
            age_hours = (now - note.get("created_at", now)) / 3600
            if age_hours < 1:
                score += 1.0 * (1 - age_hours)
            # 决策类笔记：累计检索次数
            if note.get("kind") == "decision":
                note["retrieve_count"] = note.get("retrieve_count", 0) + 1
                # 仅当 AUTO_PROMOTE=True 且达到阈值才写入 durable
                if (
                    AUTO_PROMOTE
                    and note["retrieve_count"] >= PROMOTE_THRESHOLD
                    and durable_store is not None
                ):
                    promotions.append(("key-decisions", f"Decision: {note['text']}"))
            scored.append((score, note))
    scored.sort(key=lambda x: x[0], reverse=True)

    # 执行晋升
    if promotions and durable_store is not None:
        try:
            durable_store.promote(promotions)
        except Exception:
            pass

    return [{**note, "score": round(s, 2)} for s, note in scored[:limit]]
