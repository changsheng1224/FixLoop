"""从 issue 文本提取 FAIL_TO_PASS 提示（不依赖 benchmark 包）。"""

from __future__ import annotations

import re

__all__ = [
    "FAIL_TO_PASS_HEADER",
    "extract_fail_to_pass_hints",
]

FAIL_TO_PASS_HEADER = "FAIL_TO_PASS tests (hints, may not exist locally):"
_FAIL_TO_PASS_LINE = re.compile(r"^\s*-\s+(\S.+?)\s*$")


def extract_fail_to_pass_hints(issue: str) -> list[str]:
    """解析 issue 中 FAIL_TO_PASS 段落；去重保序。"""
    if not issue or FAIL_TO_PASS_HEADER not in issue:
        return []
    hints: list[str] = []
    in_section = False
    for line in issue.splitlines():
        if line.strip() == FAIL_TO_PASS_HEADER:
            in_section = True
            continue
        if not in_section:
            continue
        if not line.strip():
            break
        if line.strip().startswith("[") or line.strip().startswith("PASS_TO_PASS"):
            break
        m = _FAIL_TO_PASS_LINE.match(line)
        if not m:
            # 允许无前缀的裸 nodeid/路径行
            raw = line.strip()
            if raw and not raw.startswith("#"):
                if raw not in hints:
                    hints.append(raw)
            continue
        hint = m.group(1).strip()
        if hint and hint not in hints:
            hints.append(hint)
    return hints
