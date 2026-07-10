"""通用 stdlib Template 渲染与 fingerprint。"""

from __future__ import annotations

import hashlib
import re
from string import Template

__all__ = [
    "render_template",
    "template_fingerprint",
    "template_metadata",
]

_TEMPLATE_META_KEYS = ("task_template_source", "task_template_fingerprint")


def template_fingerprint(template: str) -> str:
    return hashlib.sha256(template.encode()).hexdigest()


def render_template(template: str, variables: dict[str, str]) -> str:
    """safe_substitute 渲染并折叠因空变量产生的多余空行。"""
    subs = {key: value or "" for key, value in variables.items()}
    text = Template(template).safe_substitute(subs)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def template_metadata(template: str, source: str) -> dict[str, str]:
    return {
        "task_template_source": source,
        "task_template_fingerprint": template_fingerprint(template),
    }
