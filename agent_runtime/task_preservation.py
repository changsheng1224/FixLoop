"""Task / user message 保护：兼容 re-export（实现见 task_section）。"""

from __future__ import annotations

from agent_runtime.task_section import (
    issue_preserved,
    reserve_section_budget,
    task_preservation_metadata,
)

__all__ = [
    "issue_preserved",
    "reserve_section_budget",
    "task_preservation_metadata",
]
