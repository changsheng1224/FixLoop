"""仓库轻量符号索引：定义表 + 测试→实现覆盖边。

相对纯启发式 expand：
- 一次扫描缓存 defs / 测试 import 边
- 失败 nodeid / issue 符号可 O(1)~O(k) 查定义
- 不绑 instance；扫描有文件数上限
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from src.repair.localize_expand import extract_symbols_from_issue
from src.repair.localize_quality import _is_test_path, normalize_repo_path
from src.state import SuspectLocation

__all__ = [
    "RepoSymbolIndex",
    "boost_suspects_from_index",
    "get_or_build_index",
    "has_grounded_impl_suspect",
]

_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".tox",
        "dist",
        "build",
        ".eggs",
        "site-packages",
    }
)
_MAX_FILES = 350
_INDEX_CACHE: dict[str, "RepoSymbolIndex"] = {}


@dataclass(frozen=True)
class SymbolHit:
    path: str
    line: int
    kind: str
    name: str


@dataclass
class RepoSymbolIndex:
    repo_root: str
    defs: dict[str, list[SymbolHit]] = field(default_factory=dict)
    # test_rel -> imported module strings (e.g. mypkg.core)
    test_imports: dict[str, list[str]] = field(default_factory=dict)
    # module string -> impl rel paths
    module_files: dict[str, list[str]] = field(default_factory=dict)
    file_count: int = 0
    built_ms: int = 0

    def lookup(self, name: str, *, max_hits: int = 4) -> list[SymbolHit]:
        if not name:
            return []
        hits = self.defs.get(name) or []
        # 实现优先：非 test 路径在前
        ranked = sorted(hits, key=lambda h: (1 if _is_test_path(h.path) else 0, h.path))
        return [h for h in ranked if not _is_test_path(h.path)][:max_hits]

    def impls_for_test(self, test_ref: str, *, max_hits: int = 4) -> list[SuspectLocation]:
        ref = (test_ref or "").replace("\\", "/")
        file_part = ref.split("::", 1)[0]
        rel = normalize_repo_path(file_part, self.repo_root) or file_part.lstrip("./")
        mods = list(self.test_imports.get(rel) or [])
        out: list[SuspectLocation] = []
        seen: set[str] = set()
        for mod in mods:
            for path in self.module_files.get(mod, []):
                if path in seen or _is_test_path(path):
                    continue
                seen.add(path)
                # 若 nodeid 末段是符号，尝试落点
                focus = ""
                if "::" in ref:
                    focus = ref.split("::")[-1].split("[", 1)[0]
                line = 1
                fname = None
                if focus and focus in self.defs:
                    for hit in self.defs[focus]:
                        if hit.path == path:
                            line = hit.line
                            fname = focus if hit.kind != "class" else None
                            break
                out.append(
                    SuspectLocation(
                        file_path=path,
                        start_line=line,
                        end_line=line,
                        function_name=fname,
                        reason="测试覆盖边",
                        confidence=0.8,
                    )
                )
                if len(out) >= max_hits:
                    return out
        return out


def _module_name_for_file(root: Path, path: Path) -> str | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
    if parts[0] in ("src", "lib") and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    else:
        return None
    if not parts:
        return None
    return ".".join(parts)


def _iter_py_files(root: Path, *, limit: int = _MAX_FILES) -> Iterable[Path]:
    count = 0
    try:
        iterator = root.rglob("*.py")
    except OSError:
        return
    for path in iterator:
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        yield path
        count += 1
        if count >= limit:
            return


def build_symbol_index(repo_root: str | Path, *, max_files: int = _MAX_FILES) -> RepoSymbolIndex:
    root = Path(repo_root).resolve()
    t0 = time.time()
    idx = RepoSymbolIndex(repo_root=str(root))
    n_files = 0
    for path in _iter_py_files(root, limit=max_files):
        n_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(text)
            rel = str(path.relative_to(root)).replace("\\", "/")
        except (OSError, SyntaxError, ValueError):
            continue

        mod = _module_name_for_file(root, path)
        if mod and not _is_test_path(rel):
            idx.module_files.setdefault(mod, [])
            if rel not in idx.module_files[mod]:
                idx.module_files[mod].append(rel)

        imports: list[str] = []

        def add_def(name: str, lineno: int, kind: str) -> None:
            if not name or name.startswith("__"):
                return
            idx.defs.setdefault(name, []).append(
                SymbolHit(path=rel, line=int(lineno), kind=kind, name=name)
            )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_def(node.name, node.lineno, "function")
            elif isinstance(node, ast.ClassDef):
                add_def(node.name, node.lineno, "class")
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add_def(item.name, item.lineno, "method")
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name.split(".")[0])

        if _is_test_path(rel) and imports:
            # 去重保序
            seen: set[str] = set()
            ordered: list[str] = []
            for m in imports:
                if m and m not in seen:
                    seen.add(m)
                    ordered.append(m)
            idx.test_imports[rel] = ordered[:24]

    idx.file_count = n_files
    idx.built_ms = int((time.time() - t0) * 1000)
    return idx


def get_or_build_index(repo_root: str | Path) -> RepoSymbolIndex:
    key = str(Path(repo_root).resolve())
    hit = _INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    idx = build_symbol_index(key)
    _INDEX_CACHE[key] = idx
    return idx


def has_grounded_impl_suspect(
    suspects: list[SuspectLocation] | None,
    repo_root: str | Path,
) -> bool:
    """至少一个仓库内真实存在的非测试实现文件。"""
    root = Path(repo_root)
    for s in suspects or []:
        rel = normalize_repo_path(getattr(s, "file_path", "") or "", root)
        if not rel or _is_test_path(rel):
            continue
        if (root / rel).is_file():
            return True
    return False


def boost_suspects_from_index(
    *,
    repo_root: str | Path,
    issue: str = "",
    related_tests: list[str] | None = None,
    fail_nodeids: list[str] | None = None,
    max_new: int = 8,
) -> list[SuspectLocation]:
    """用索引从测试覆盖边 + issue 符号补实现嫌疑。"""
    try:
        idx = get_or_build_index(repo_root)
    except Exception:
        return []

    out: list[SuspectLocation] = []
    seen: set[str] = set()

    def push(s: SuspectLocation) -> None:
        rel = normalize_repo_path(s.file_path or "", repo_root)
        if not rel or rel in seen or _is_test_path(rel):
            return
        if not (Path(repo_root) / rel).is_file():
            return
        seen.add(rel)
        out.append(
            SuspectLocation(
                file_path=rel,
                start_line=int(s.start_line or 1),
                end_line=max(int(s.start_line or 1), int(s.end_line or 1)),
                function_name=s.function_name,
                class_name=s.class_name,
                reason=s.reason or "符号索引",
                confidence=float(s.confidence or 0.75),
            )
        )

    for ref in list(fail_nodeids or []) + list(related_tests or []):
        for s in idx.impls_for_test(str(ref)):
            push(s)
            if len(out) >= max_new:
                return out

    for sym in extract_symbols_from_issue(issue or "", limit=10):
        for hit in idx.lookup(sym, max_hits=2):
            push(
                SuspectLocation(
                    file_path=hit.path,
                    start_line=hit.line,
                    end_line=hit.line,
                    function_name=hit.name if hit.kind != "class" else None,
                    class_name=hit.name if hit.kind == "class" else None,
                    reason="符号索引",
                    confidence=0.76,
                )
            )
            if len(out) >= max_new:
                return out
    return out
