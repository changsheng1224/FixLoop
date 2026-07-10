"""从 issue 文本提取文件路径（language_detect 与 Orchestrator 共用）。"""

from __future__ import annotations

import re

_PATH_IN_ISSUE_RE = re.compile(
    r'File\s+"([^"]+)"|at\s+(\S+\.\w+)',
)


def extract_paths_from_issue(
    issue: str,
    *,
    extra: list[str] | None = None,
) -> list[str]:
    """去重保序合并 ``extra`` 与 issue 中的 ``File "..."`` / ``at path``。"""
    paths: list[str] = []
    for raw in list(extra or []):
        path = raw.replace("\\", "/")
        if path and path not in paths:
            paths.append(path)
    for file_match, at_match in _PATH_IN_ISSUE_RE.findall(issue):
        path = (file_match or at_match).replace("\\", "/")
        if path and path not in paths:
            paths.append(path)
    return paths
