"""符号/行级落点：把粗粒度嫌疑对齐到 defs。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import SuspectLocation

__all__ = ["refine_suspect_landing"]


def refine_suspect_landing(
    suspects: list["SuspectLocation"] | None,
    repo_root: str | Path,
    issue: str = "",
    *,
    related_tests: list[str] | None = None,
    fail_nodeids: list[str] | None = None,
) -> list["SuspectLocation"]:
    """对 start_line==1 且无 function 的实现嫌疑，用索引符号对齐行号。"""
    from src.repair.localize_expand import extract_symbols_from_issue
    from src.repair.localize_quality import _is_test_path, normalize_repo_path
    from src.repair.symbol_index import get_or_build_index
    from src.state import SuspectLocation

    if not suspects:
        return []
    root = Path(repo_root)
    idx = get_or_build_index(root)
    symbols = extract_symbols_from_issue(issue or "", limit=12)
    for ref in list(related_tests or []) + list(fail_nodeids or []):
        if "::" in ref:
            tail = ref.split("::")[-1].split("[", 1)[0]
            if tail and tail not in symbols:
                symbols.append(tail)

    out: list[SuspectLocation] = []
    for s in suspects:
        rel = normalize_repo_path(s.file_path or "", root) or (
            s.file_path or ""
        ).replace("\\", "/")
        if not rel or _is_test_path(rel):
            out.append(s)
            continue
        start = int(s.start_line or 1)
        func = s.function_name
        if (start > 1 and func) or not (root / rel).is_file():
            out.append(s)
            continue

        # 优先：已有 function_name 查 defs
        new_line = start
        new_func = func
        if func and func in idx.defs:
            for hit in idx.defs[func]:
                if hit.path == rel:
                    new_line = hit.line
                    break
        elif symbols:
            for sym in symbols:
                hits = [h for h in (idx.defs.get(sym) or []) if h.path == rel]
                if hits:
                    new_line = hits[0].line
                    new_func = sym if hits[0].kind != "class" else func
                    break

        out.append(
            SuspectLocation(
                file_path=rel,
                start_line=new_line,
                end_line=max(new_line, int(s.end_line or new_line)),
                function_name=new_func,
                class_name=s.class_name,
                reason=s.reason,
                confidence=float(s.confidence or 0.0),
            )
        )
    return out
