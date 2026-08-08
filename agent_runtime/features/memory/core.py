"""记忆系统核心：状态初始化、规范化、常量。"""

from pathlib import Path

MAX_RECENT_FILES = 8
MAX_FILE_SUMMARIES = 6
MAX_EPISODIC_NOTES = 12
MAX_EVIDENCE_ENTRIES = 10


def default_memory_state() -> dict:
    """返回初始记忆结构。"""
    return {
        "working": {
            "task_summary": "",
            "repair_context": {},
            "recent_files": [],
            "evidence_ledger": [],
            "read_cache": {},
        },
        "episodic_notes": [],
        "file_summaries": {},
        "next_note_index": 0,
        "memory_identity": {"user_id": "", "task_id": ""},
        "recalled_memory_ids": [],
        "memory_usage_events": [],
        "governed_memories": {},
        "memory_policies": {},
        "memory_conflicts": {},
        "memory_governance_audit": [],
        "memory_revalidation_queue": [],
    }


def normalize_memory_state(state: dict, workspace_root: str) -> dict:
    """规范化记忆状态：兼容旧格式，裁剪超限条目。"""
    if not isinstance(state, dict):
        return default_memory_state()
    if "working" not in state:
        state["working"] = {
            "task_summary": "",
            "recent_files": [],
            "evidence_ledger": [],
            "read_cache": {},
        }
    working = state["working"]
    if not isinstance(working, dict):
        working = {
            "task_summary": "",
            "recent_files": [],
            "evidence_ledger": [],
            "read_cache": {},
        }
        state["working"] = working
    working.setdefault("task_summary", "")
    working.setdefault("repair_context", {})
    working.setdefault("recent_files", [])
    working.setdefault("evidence_ledger", [])
    working.setdefault("read_cache", {})
    working["recent_files"] = _filter_existing(
        working["recent_files"][:MAX_RECENT_FILES], workspace_root
    )
    ledger = working["evidence_ledger"]
    if isinstance(ledger, list):
        working["evidence_ledger"] = ledger[-MAX_EVIDENCE_ENTRIES:]
    else:
        working["evidence_ledger"] = []
    if not isinstance(working["read_cache"], dict):
        working["read_cache"] = {}
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
    identity = state.setdefault("memory_identity", {"user_id": "", "task_id": ""})
    if not isinstance(identity, dict):
        state["memory_identity"] = {"user_id": "", "task_id": ""}
    state.setdefault("recalled_memory_ids", [])
    if not isinstance(state.get("memory_usage_events"), list):
        state["memory_usage_events"] = []
    state.setdefault("governed_memories", {})
    state.setdefault("memory_policies", {})
    state.setdefault("memory_conflicts", {})
    state.setdefault("memory_governance_audit", [])
    if not isinstance(state.get("memory_revalidation_queue"), list):
        state["memory_revalidation_queue"] = []
    return state


def set_memory_identity(state: dict, *, user_id: str = "", task_id: str = "") -> dict:
    """Set the caller boundary used by governed recall and feedback."""
    state["memory_identity"] = {
        "user_id": str(user_id or ""),
        "task_id": str(task_id or ""),
    }
    return state["memory_identity"]


def _filter_existing(paths: list[str], root: str) -> list[str]:
    result = []
    for p in paths:
        if (Path(root) / p).exists():
            result.append(p)
    return result
