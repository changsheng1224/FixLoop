"""三层记忆系统：Working Memory + Episodic Memory + Durable Memory。

Working Memory：当前任务上下文（容量有限，频繁读写）
Episodic Memory：本轮会话的事件笔记（容量有限，FIFO 淘汰）
Durable Memory：跨会话持久记忆（M3D2 实现，读写 Markdown 文件）
"""

import time
from pathlib import Path

# ============================================================================
# 常量
# ============================================================================

MAX_RECENT_FILES = 8
MAX_FILE_SUMMARIES = 6
MAX_EPISODIC_NOTES = 12


# ============================================================================
# 初始状态与规范化
# ============================================================================


def default_memory_state() -> dict:
    """返回初始记忆结构。

    Returns:
        {
            "working": {"task_summary": "", "recent_files": []},
            "episodic_notes": [],
            "file_summaries": {},
            "next_note_index": 0,
        }
    """
    return {
        "working": {
            "task_summary": "",
            "recent_files": [],
        },
        "episodic_notes": [],
        "file_summaries": {},
        "next_note_index": 0,
    }


def normalize_memory_state(state: dict, workspace_root: str) -> dict:
    """规范化记忆状态：兼容旧格式，裁剪超限条目。

    Args:
        state: 当前记忆状态（可能为 None 或不完整）。
        workspace_root: workspace 根目录（用于过滤已删除的文件）。

    Returns:
        规范化后的记忆状态。
    """
    if not isinstance(state, dict):
        return default_memory_state()

    # 确保 working 层存在
    if "working" not in state:
        state["working"] = {"task_summary": "", "recent_files": []}
    working = state["working"]
    if not isinstance(working, dict):
        working = {"task_summary": "", "recent_files": []}
        state["working"] = working

    # 确保字段存在
    working.setdefault("task_summary", "")
    working.setdefault("recent_files", [])

    # 裁剪 recent_files（只保留 8 个且文件存在）
    working["recent_files"] = _filter_existing(
        working["recent_files"][:MAX_RECENT_FILES], workspace_root
    )

    # 确保 episodic_notes
    if "episodic_notes" not in state:
        state["episodic_notes"] = []
    state["episodic_notes"] = state["episodic_notes"][:MAX_EPISODIC_NOTES]

    # 确保 file_summaries
    if "file_summaries" not in state:
        state["file_summaries"] = {}
    # 裁剪到 MAX_FILE_SUMMARIES，保留最新的
    summaries = state["file_summaries"]
    if isinstance(summaries, dict) and len(summaries) > MAX_FILE_SUMMARIES:
        # 按 freshness 排序，保留最新 6 个
        sorted_items = sorted(
            summaries.items(),
            key=lambda x: x[1].get("created_at", 0) if isinstance(x[1], dict) else 0,
            reverse=True,
        )
        state["file_summaries"] = dict(sorted_items[:MAX_FILE_SUMMARIES])

    state.setdefault("next_note_index", 0)

    return state


def _filter_existing(paths: list[str], root: str) -> list[str]:
    """过滤掉不存在的文件路径。"""
    root_path = Path(root)
    result = []
    for p in paths:
        if (root_path / p).exists():
            result.append(p)
    return result


# ============================================================================
# Working Memory 层
# ============================================================================


def set_task_summary(state: dict, user_message: str):
    """设置当前任务的一句话摘要（截断到 300 字符）。

    Args:
        state: 记忆状态。
        user_message: 用户输入，用于生成摘要。
    """
    summary = user_message.strip().replace("\n", " ")[:300]
    state["working"]["task_summary"] = summary


def remember_file(state: dict, path: str):
    """记录一个文件到 recent_files（去重 + LRU + trim 到 8）。

    Args:
        state: 记忆状态。
        path: 文件相对路径。
    """
    files = state["working"]["recent_files"]
    # 去重：已存在则移到末尾
    if path in files:
        files.remove(path)
    files.append(path)
    # Trim
    state["working"]["recent_files"] = files[-MAX_RECENT_FILES:]


def set_file_summary(state: dict, path: str, summary: str):
    """存储文件摘要（带 freshness hash）。

    Args:
        state: 记忆状态。
        path: 文件路径。
        summary: 文件内容摘要（取前 180 字符）。
    """
    state["file_summaries"][path] = {
        "summary": summary[:180],
        "created_at": time.time(),
        "freshness": _freshness_hash(path),
    }


def invalidate_file_summary(state: dict, path: str):
    """删除文件的摘要缓存（文件被修改后失效）。

    Args:
        state: 记忆状态。
        path: 文件路径。
    """
    state["file_summaries"].pop(path, None)


def _freshness_hash(path: str) -> str:
    """计算文件的 freshness hash（基于 mtime + size）。"""
    try:
        p = Path(path)
        if p.exists():
            stat = p.stat()
            return f"{stat.st_mtime}:{stat.st_size}"
    except OSError:
        pass
    return ""


# ============================================================================
# Episodic Memory 层
# ============================================================================


def append_note(
    state: dict,
    text: str,
    tags: list[str] | None = None,
    source: str = "",
    kind: str = "observation",
):
    """追加一条事件笔记（dedupe by text + trim 到 12 条）。

    Args:
        state: 记忆状态。
        text: 笔记内容。
        tags: 标签列表。
        source: 来源（如文件路径）。
        kind: 类型: "observation" | "error" | "decision"。
    """
    if tags is None:
        tags = []

    # Dedupe：如果最后一条笔记内容完全相同，不重复添加
    notes = state["episodic_notes"]
    if notes and notes[-1].get("text") == text:
        return

    note_index = state["next_note_index"]
    state["next_note_index"] = note_index + 1

    note = {
        "text": text[:300],  # 截断
        "tags": tags[:5],    # 最多 5 个 tag
        "source": source,
        "created_at": time.time(),
        "note_index": note_index,
        "kind": kind,
    }
    notes.append(note)

    # Trim 到 12 条（FIFO）
    if len(notes) > MAX_EPISODIC_NOTES:
        state["episodic_notes"] = notes[-MAX_EPISODIC_NOTES:]


def retrieval_candidates(state: dict, query: str, limit: int = 3) -> list[dict]:
    """检索与查询相关的记忆条目。

    排序策略：tag 精确匹配 > keyword 重叠 > recency 时间衰减。

    Args:
        state: 记忆状态。
        query: 搜索查询。
        limit: 返回条数上限。

    Returns:
        相关笔记列表（按相关性降序）。
    """
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

        # Tag 精确匹配（权重最高）
        for token in query_tokens:
            if token in tags:
                score += 3.0
            # 文本关键词重叠
            if token in text:
                score += 1.0

        # Tag 子串匹配
        for tag in tags:
            if tag in query_lower:
                score += 2.0

        # Recency 时间衰减（1 小时内的笔记加分，仅当已有相关性）
        if score > 0:
            age_hours = (now - note.get("created_at", now)) / 3600
            if age_hours < 1:
                score += 1.0 * (1 - age_hours)

        if score > 0:
            scored.append((score, note))

    # 按分数降序，取 top_k
    scored.sort(key=lambda x: x[0], reverse=True)
    return [note for _, note in scored[:limit]]
