"""工作记忆 — Working Memory：当前任务 + 最近文件 + 文件摘要。"""

import time
from pathlib import Path

from agent_runtime.features.memory.core import MAX_RECENT_FILES


def set_task_summary(state: dict, user_message: str):
    """将用户请求截断写入 working.task_summary。"""
    summary = user_message.strip().replace("\n", " ")[:300]
    state["working"]["task_summary"] = summary


def remember_file(state: dict, path: str):
    """将 path 加入 recent_files LRU 列表。"""
    files = state["working"]["recent_files"]
    if path in files:
        files.remove(path)
    files.append(path)
    state["working"]["recent_files"] = files[-MAX_RECENT_FILES:]


def set_file_summary(state: dict, path: str, summary: str):
    """缓存文件摘要及 freshness hash。"""
    state["file_summaries"][path] = {
        "summary": summary[:180],
        "created_at": time.time(),
        "freshness": _freshness_hash(path),
    }


def invalidate_file_summary(state: dict, path: str):
    """文件变更后清除过期摘要。"""
    state["file_summaries"].pop(path, None)


def _freshness_hash(path: str) -> str:
    try:
        p = Path(path)
        if p.exists():
            stat = p.stat()
            return f"{stat.st_mtime}:{stat.st_size}"
    except OSError:
        pass
    return ""
