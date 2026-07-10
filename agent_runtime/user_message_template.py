"""User Message 任务段模板：兼容 re-export（实现见 task_section / template_render）。"""

from __future__ import annotations

from agent_runtime.task_section import (
    DEFAULT_TASK_TEMPLATE,
    TASK_TEMPLATE_FILENAME,
    load_task_template,
    render_task_message,
)
from agent_runtime.template_render import render_template, template_fingerprint

__all__ = [
    "DEFAULT_TASK_TEMPLATE",
    "TASK_TEMPLATE_FILENAME",
    "load_task_template",
    "render_task_message",
    "render_template",
    "template_fingerprint",
]
