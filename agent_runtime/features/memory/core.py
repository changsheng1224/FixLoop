"""记忆系统核心：状态初始化与常量。"""

MAX_RECENT_FILES = 8
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


def set_memory_identity(state: dict, *, user_id: str = "", task_id: str = "") -> dict:
    """Set the caller boundary used by governed recall and feedback."""
    state["memory_identity"] = {
        "user_id": str(user_id or ""),
        "task_id": str(task_id or ""),
    }
    return state["memory_identity"]
