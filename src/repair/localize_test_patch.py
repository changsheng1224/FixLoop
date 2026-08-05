"""从官方 test_patch 文本反推实现嫌疑（不落盘、不绑 instance）。"""

from __future__ import annotations

import re
from pathlib import Path

from src.state import SuspectLocation

__all__ = [
    "import_hints_from_test_patch",
    "suspects_from_test_patch",
]

_IMPORT_FROM = re.compile(
    r"^\+\s*from\s+([\w.]+)\s+import\s+(.+)$",
    re.MULTILINE,
)
_IMPORT = re.compile(r"^\+\s*import\s+([\w.]+)", re.MULTILINE)
_DEF = re.compile(r"^\+\s*def\s+(\w+)\s*\(", re.MULTILINE)
_NAME_TOKEN = re.compile(r"[A-Za-z_][\w]*")


def import_hints_from_test_patch(patch_text: str) -> list[tuple[str, str]]:
    """返回 (module, name) 列表；name 可为 '*' 或空。"""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    text = patch_text or ""
    for m in _IMPORT_FROM.finditer(text):
        mod = m.group(1).strip()
        names_raw = m.group(2)
        for part in names_raw.split(","):
            part = part.strip()
            if not part or part.startswith("("):
                continue
            name = part.split(" as ", 1)[0].strip()
            if name == "*":
                key = (mod, "")
            else:
                key = (mod, name)
            if key not in seen:
                seen.add(key)
                out.append(key)
    for m in _IMPORT.finditer(text):
        mod = m.group(1).strip()
        key = (mod, "")
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def suspects_from_test_patch(
    patch_text: str,
    repo_root: str | Path,
    *,
    max_keep: int = 8,
) -> list[SuspectLocation]:
    """test_patch 路径 + 新增 import/符号 → 实现文件嫌疑。"""
    if not (patch_text or "").strip():
        return []

    from src.repair.localize_quality import _is_test_path, normalize_repo_path
    from src.repair.symbol_index import get_or_build_index
    from src.repair.verify_test_patch import iter_test_patch_paths

    root = Path(repo_root)
    idx = get_or_build_index(root)
    out: list[SuspectLocation] = []
    seen: set[str] = set()

    def _add(path: str, *, line: int = 1, func: str | None = None, conf: float = 0.85) -> None:
        rel = normalize_repo_path(path, root) or path.replace("\\", "/").lstrip("./")
        if not rel or rel in seen or _is_test_path(rel):
            return
        if not (root / rel).is_file():
            return
        seen.add(rel)
        out.append(
            SuspectLocation(
                file_path=rel,
                start_line=max(1, int(line)),
                end_line=max(1, int(line)),
                function_name=func,
                reason="test_patch覆盖",
                confidence=conf,
            )
        )

    # 1) 测试文件路径 → 覆盖边
    test_paths = iter_test_patch_paths(patch_text)
    for rel in test_paths:
        for s in idx.impls_for_test(rel, max_hits=4):
            _add(
                s.file_path,
                line=s.start_line,
                func=s.function_name,
                conf=max(0.85, float(s.confidence or 0.0)),
            )
            if len(out) >= max_keep:
                return out

    # 2) 新增 import → module_files / defs
    for mod, name in import_hints_from_test_patch(patch_text):
        for path in idx.module_files.get(mod, []):
            line = 1
            func = None
            if name and name in idx.defs:
                for hit in idx.defs[name]:
                    if hit.path == path:
                        line = hit.line
                        func = name if hit.kind != "class" else None
                        break
            _add(path, line=line, func=func, conf=0.88)
            if len(out) >= max_keep:
                return out
        # 部分 import 只有顶层包名
        if "." in mod:
            parent = mod.rsplit(".", 1)[0]
            for path in idx.module_files.get(parent, [])[:2]:
                _add(path, conf=0.7)
                if len(out) >= max_keep:
                    return out

    # 3) 新增 def test_* 旁的符号名 → defs
    for m in _DEF.finditer(patch_text or ""):
        # already handled as tests; skip
        _ = m
    for tok in _NAME_TOKEN.findall(patch_text or ""):
        if tok.startswith("test_") or tok in ("self", "cls", "True", "False", "None"):
            continue
        if tok[0].islower() and "_" not in tok and len(tok) < 4:
            continue
        hits = idx.lookup(tok, max_hits=2)
        for hit in hits:
            _add(
                hit.path,
                line=hit.line,
                func=tok if hit.kind != "class" else None,
                conf=0.8,
            )
            if len(out) >= max_keep:
                return out
        if len(out) >= max_keep:
            break

    return out[:max_keep]
