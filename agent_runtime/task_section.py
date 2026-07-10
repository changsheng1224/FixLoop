"""Task 段渲染与预算预留保护。"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.template_render import render_template, template_metadata

__all__ = [
    "DEFAULT_TASK_TEMPLATE",
    "TASK_TEMPLATE_FILENAME",
    "issue_preserved",
    "load_task_template",
    "render_task_message",
    "reserve_section_budget",
    "task_preservation_metadata",
]

TASK_TEMPLATE_FILENAME = "task_template.md"
DEFAULT_TASK_TEMPLATE = "## 当前任务\n\n$task"


def reserve_section_budget(total_limit: int, request_tokens: int) -> int:
    """为 task/request 预留后，其余 section 可用 token 上限。"""
    return max(0, total_limit - request_tokens)


def task_preservation_metadata(request_tokens: int, total_limit: int) -> dict[str, bool]:
    meta: dict[str, bool] = {"request_preserved": True}
    if request_tokens > total_limit:
        meta["task_budget_overflow"] = True
    return meta


def issue_preserved(original: str, rendered: str) -> bool:
    if not original:
        return True
    return original in (rendered or "")


def load_task_template(repo_root: str | Path | None = None) -> tuple[str, str]:
    if repo_root is not None:
        path = Path(repo_root) / ".agent" / TASK_TEMPLATE_FILENAME
        if path.is_file():
            return path.read_text(encoding="utf-8").strip(), f"repo:.agent/{TASK_TEMPLATE_FILENAME}"
    return DEFAULT_TASK_TEMPLATE, "builtin"


def render_task_message(
    task: str,
    *,
    repo_root: str | Path | None = None,
    refs: str = "",
) -> tuple[str, dict]:
    template, source = load_task_template(repo_root)
    rendered = render_template(template, {"task": task, "refs": refs})
    return rendered, template_metadata(template, source)
