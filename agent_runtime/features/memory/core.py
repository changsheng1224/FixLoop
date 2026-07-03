"""记忆系统核心：状态初始化、规范化、常量。"""

from pathlib import Path

MAX_RECENT_FILES = 8
MAX_FILE_SUMMARIES = 6
MAX_EPISODIC_NOTES = 12


def default_memory_state() -> dict:
    """返回初始记忆结构。"""
    return {
        "working": {"task_summary": "", "recent_files": []},
        "episodic_notes": [],
        "file_summaries": {},
        "next_note_index": 0,
    }


def normalize_memory_state(state: dict, workspace_root: str) -> dict:
    """规范化记忆状态：兼容旧格式，裁剪超限条目。"""
    if not isinstance(state, dict):
        return default_memory_state()
    if "working" not in state:
        state["working"] = {"task_summary": "", "recent_files": []}
    working = state["working"]
    if not isinstance(working, dict):
        working = {"task_summary": "", "recent_files": []}
        state["working"] = working
    working.setdefault("task_summary", "")
    working.setdefault("recent_files", [])
    working["recent_files"] = _filter_existing(
        working["recent_files"][:MAX_RECENT_FILES], workspace_root
    )
    if "episodic_notes" not in state:
        state["episodic_notes"] = []
    state["episodic_notes"] = state["episodic_notes"][:MAX_EPISODIC_NOTES]
    if "file_summaries" not in state:
        state["file_summaries"] = {}
    summaries = state["file_summaries"]
    if isinstance(summaries, dict) and len(summaries) > MAX_FILE_SUMMARIES:
        sorted_items = sorted(
            summaries.items(),
            key=lambda x: x[1].get("created_at", 0) if isinstance(x[1], dict) else 0,
            reverse=True,
        )
        state["file_summaries"] = dict(sorted_items[:MAX_FILE_SUMMARIES])
    state.setdefault("next_note_index", 0)
    return state


def _filter_existing(paths: list[str], root: str) -> list[str]:
    result = []
    for p in paths:
        if (Path(root) / p).exists():
            result.append(p)
    return result
