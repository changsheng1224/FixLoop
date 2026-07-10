"""User Message 任务段模板：stdlib Template + 可选 repo 外置。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from string import Template

__all__ = [
    "DEFAULT_TASK_TEMPLATE",
    "TASK_TEMPLATE_FILENAME",
    "load_task_template",
    "render_task_message",
    "render_template",
    "template_fingerprint",
]

TASK_TEMPLATE_FILENAME = "task_template.md"
DEFAULT_TASK_TEMPLATE = "## 当前任务\n\n$task"


def template_fingerprint(template: str) -> str:
    return hashlib.sha256(template.encode()).hexdigest()


def load_task_template(repo_root: str | Path | None = None) -> tuple[str, str]:
    """加载任务模板，返回 (template_text, source)。"""
    if repo_root is not None:
        path = Path(repo_root) / ".agent" / TASK_TEMPLATE_FILENAME
        if path.is_file():
            return path.read_text(encoding="utf-8").strip(), f"repo:.agent/{TASK_TEMPLATE_FILENAME}"
    return DEFAULT_TASK_TEMPLATE, "builtin"


def render_template(template: str, variables: dict[str, str]) -> str:
    """safe_substitute 渲染并折叠因空变量产生的多余空行。"""
    subs = {key: value or "" for key, value in variables.items()}
    text = Template(template).safe_substitute(subs)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def render_task_message(
    task: str,
    *,
    repo_root: str | Path | None = None,
    refs: str = "",
) -> tuple[str, dict]:
    """渲染 L1 task 段全文（含标题），返回 (text, template_metadata)。"""
    template, source = load_task_template(repo_root)
    rendered = render_template(template, {"task": task, "refs": refs})
    return rendered, {
        "task_template_source": source,
        "task_template_fingerprint": template_fingerprint(template),
    }
