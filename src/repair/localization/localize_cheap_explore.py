"""空锚时强制 cheap grep 探仓（不调 localize LLM）。"""

from __future__ import annotations

import re
from pathlib import Path

from src.state import SuspectLocation

__all__ = ["cheap_explore_suspects"]


def cheap_explore_suspects(
    issue: str,
    repo_root: str | Path,
    *,
    max_keep: int = 6,
    max_keywords: int = 6,
    max_results_per_kw: int = 8,
) -> list[SuspectLocation]:
    """用 issue 关键词 / 符号做有限次 grep，只收仓库内非测试 .py。"""
    from agent_runtime.tool_context import ToolContext
    from agent_runtime.tools import tool_grep
    from src.repair.localization.localize_expand import extract_symbols_from_issue
    from src.repair.localization.localize_quality import (
        _is_test_path,
        normalize_repo_path,
        retrieve_keywords,
    )

    root = Path(repo_root)
    keywords = retrieve_keywords([], issue or "", max_keywords=max_keywords)
    for sym in extract_symbols_from_issue(issue or "", limit=8):
        if sym and sym not in keywords:
            keywords.append(sym)
        if len(keywords) >= max_keywords:
            break
    if not keywords:
        return []

    ctx = ToolContext(root=str(root))
    out: list[SuspectLocation] = []
    seen: set[str] = set()

    for kw in keywords:
        pattern = rf"\b{re.escape(kw)}\b"
        try:
            grep_out = tool_grep(
                ctx,
                {
                    "pattern": pattern,
                    "path": ".",
                    "glob": "*.py",
                    "max_results": max_results_per_kw,
                },
            )
        except Exception:
            continue
        if not grep_out or grep_out.startswith("Error") or grep_out == "(无匹配)":
            continue
        for line in grep_out.splitlines():
            # path:lineno:text 或 path:text
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            raw_path = parts[0].strip()
            lineno = 1
            if len(parts) >= 3 and parts[1].strip().isdigit():
                lineno = int(parts[1].strip())
            rel = normalize_repo_path(raw_path, root)
            if not rel or rel in seen or _is_test_path(rel):
                continue
            if not (root / rel).is_file():
                continue
            seen.add(rel)
            out.append(
                SuspectLocation(
                    file_path=rel,
                    start_line=lineno,
                    end_line=lineno,
                    function_name=kw if kw.isidentifier() else None,
                    reason="grep命中",
                    confidence=0.58,
                )
            )
            if len(out) >= max_keep:
                return out
    return out
