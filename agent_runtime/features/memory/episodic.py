"""事件记忆 — Episodic Memory：会话级工具执行笔记。"""

import time

from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES


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
    }
    notes.append(note)
    if len(notes) > MAX_EPISODIC_NOTES:
        state["episodic_notes"] = notes[-MAX_EPISODIC_NOTES:]


def retrieval_candidates(state: dict, query: str, limit: int = 3) -> list[dict]:
    """按关键词/tag 对 episodic notes 打分排序，返回 top-k。"""
    query_lower = query.lower()
    query_tokens = set(query_lower.split())
    notes = state.get("episodic_notes", [])
    if not notes:
        return []
    scored = []
    now = time.time()
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
            age_hours = (now - note.get("created_at", now)) / 3600
            if age_hours < 1:
                score += 1.0 * (1 - age_hours)
            scored.append((score, note))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{**note, "score": round(s, 2)} for s, note in scored[:limit]]
