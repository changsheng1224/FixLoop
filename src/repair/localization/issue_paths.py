"""从 issue 文本提取文件路径（language_detect 与 Orchestrator 共用）。"""

from __future__ import annotations

import re

_PATH_IN_ISSUE_RE = re.compile(
    r'File\s+"([^"]+)"'
    r"|at\s+(\S+\.\w+)"
    r"|((?:[\w.-]+/)+[\w.-]+\.py)(?::(\d+))?"
    r"|`((?:[\w.-]+/)+[\w.-]+\.py)`",
)


def extract_paths_from_issue(
    issue: str,
    *,
    extra: list[str] | None = None,
) -> list[str]:
    """去重保序合并 ``extra`` 与 issue 中的路径线索。"""
    paths: list[str] = []
    for raw in list(extra or []):
        path = raw.replace("\\", "/")
        if path and path not in paths:
            paths.append(path)
    for m in _PATH_IN_ISSUE_RE.finditer(issue or ""):
        file_q, at_path, rel_path, _line, tick_path = m.groups()
        path = (file_q or at_path or rel_path or tick_path or "").replace("\\", "/")
        if path and path not in paths:
            paths.append(path)
    return paths
