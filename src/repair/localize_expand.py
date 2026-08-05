"""定位天花板：多跳语义扩展（测试→导入/调用→定义；符号→定义；调用方）。

在栈接地之上抬升定位：不绑 instance，只用 AST/路径启发式。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable

from src.repair.localize_quality import _is_test_path, normalize_repo_path
from src.state import SuspectLocation

__all__ = [
    "expand_suspects_semantic",
    "extract_symbols_from_issue",
    "find_definitions",
    "symbols_from_python_file",
]

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
_CAMEL_RE = re.compile(r"\b([A-Z][A-Za-z0-9]{2,})\b")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "from",
        "this",
        "that",
        "with",
        "true",
        "false",
        "none",
        "self",
        "test",
        "tests",
        "error",
        "exception",
        "traceback",
        "file",
        "line",
        "return",
        "import",
        "class",
        "def",
        "args",
        "kwargs",
        "assert",
        "raises",
        "pytest",
        "mock",
        "patch",
        "type",
        "value",
        "object",
        "string",
        "list",
        "dict",
        "int",
        "str",
        "bool",
        "float",
        "print",
        "len",
        "range",
        "super",
        "isinstance",
        "hasattr",
        "getattr",
        "setattr",
    }
)
_SKIP_DIR_NAMES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "dist", "build"}
)


def extract_symbols_from_issue(issue: str, *, limit: int = 12) -> list[str]:
    """从 issue/失败日志抽候选符号（snake/Camel），抑制停用词。"""
    text = issue or ""
    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_CAMEL_RE, _IDENT_RE):
        for m in pattern.finditer(text):
            name = m.group(1)
            if name.lower() in _STOP or name in seen:
                continue
            if name.startswith("test_"):
                continue
            seen.add(name)
            found.append(name)
            if len(found) >= limit:
                return found
    return found


def symbols_from_python_file(
    path: Path,
    *,
    focus_func: str = "",
    max_symbols: int = 20,
) -> tuple[list[str], list[tuple[str, str]]]:
    """解析测试/源文件：返回 (names, imports as (module, name))."""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return [], []

    imports: list[tuple[str, str]] = []
    names: list[str] = []
    seen: set[str] = set()

    def add_name(n: str) -> None:
        if not n or n.lower() in _STOP or n in seen or n.startswith("__"):
            return
        seen.add(n)
        names.append(n)

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                imports.append((node.module, alias.name))
                add_name(local.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imports.append((alias.name, local))
                add_name(local)

    # 聚焦某测试函数体内的调用名
    targets: list[ast.AST] = []
    if focus_func:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == focus_func:
                targets.append(node)
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name == focus_func
                    ):
                        targets.append(item)
    if not targets:
        targets = list(tree.body)

    for root in targets:
        for node in ast.walk(root):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    add_name(func.id)
                elif isinstance(func, ast.Attribute):
                    add_name(func.attr)
                    if isinstance(func.value, ast.Name):
                        add_name(func.value.id)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                add_name(node.id)
            if len(names) >= max_symbols:
                break
    return names[:max_symbols], imports


def _module_to_candidates(repo_root: Path, module: str) -> list[Path]:
    parts = module.replace(".", "/").strip("/")
    if not parts:
        return []
    cands = [
        repo_root / f"{parts}.py",
        repo_root / parts / "__init__.py",
    ]
    # 常见 src/lib 布局
    for prefix in ("src", "lib"):
        cands.append(repo_root / prefix / f"{parts}.py")
        cands.append(repo_root / prefix / parts / "__init__.py")
    return [p for p in cands if p.is_file()]


def _iter_py_files(root: Path, prefer_dirs: list[Path], *, limit: int = 80) -> Iterable[Path]:
    seen: set[Path] = set()
    for base in prefer_dirs + [root]:
        if not base.exists():
            continue
        try:
            iterator = base.rglob("*.py") if base.is_dir() else []
        except OSError:
            continue
        for path in iterator:
            if path in seen:
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            seen.add(path)
            yield path
            if len(seen) >= limit:
                return


def find_definitions(
    repo_root: str | Path,
    name: str,
    *,
    prefer_dirs: list[str] | None = None,
    max_hits: int = 3,
) -> list[tuple[str, int, str]]:
    """在仓库中找 ``def/class name``，返回 (rel_path, lineno, kind)。"""
    root = Path(repo_root)
    if not name or name.lower() in _STOP:
        return []
    prefs = [root / d for d in (prefer_dirs or []) if d]
    hits: list[tuple[str, int, str]] = []

    for path in _iter_py_files(root, prefs, limit=100):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if f"def {name}" not in text and f"class {name}" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            kind = ""
            lineno = 0
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                kind, lineno = "function", int(node.lineno)
            elif isinstance(node, ast.ClassDef) and node.name == name:
                kind, lineno = "class", int(node.lineno)
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name == name
                    ):
                        kind, lineno = "method", int(item.lineno)
                        break
            if not kind:
                continue
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if _is_test_path(rel):
                continue
            hits.append((rel, lineno, kind))
            if len(hits) >= max_hits:
                return hits
    return hits


def _find_callers(
    repo_root: Path,
    func_name: str,
    *,
    prefer_dirs: list[Path],
    exclude_files: set[str],
    max_hits: int = 3,
) -> list[SuspectLocation]:
    if not func_name or func_name.lower() in _STOP:
        return []
    needle = f"{func_name}("
    out: list[SuspectLocation] = []
    for path in _iter_py_files(repo_root, prefer_dirs, limit=80):
        try:
            rel = str(path.relative_to(repo_root)).replace("\\", "/")
        except ValueError:
            continue
        if _is_test_path(rel):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if needle not in line:
                continue
            if re.search(rf"\bdef\s+{re.escape(func_name)}\b", line):
                continue
            # 定义文件内的其它调用仍保留；仅跳过纯定义行
            out.append(
                SuspectLocation(
                    file_path=rel,
                    start_line=i,
                    end_line=i,
                    function_name=None,
                    reason="调用方扩展",
                    confidence=0.62 if rel not in exclude_files else 0.58,
                )
            )
            break
        if len(out) >= max_hits:
            break
    return out


def expand_suspects_semantic(
    suspects: list[SuspectLocation] | None,
    *,
    repo_root: str | Path,
    issue: str = "",
    related_tests: list[str] | None = None,
    fail_nodeids: list[str] | None = None,
    max_new: int = 6,
) -> list[SuspectLocation]:
    """多跳扩展：失败测试/相关测试 → 导入与调用 → 定义；再补调用方。"""
    root = Path(repo_root)
    seeds = list(suspects or [])
    existing_files = {
        normalize_repo_path(s.file_path or "", root) or ""
        for s in seeds
    }
    existing_files.discard("")

    # 收集测试入口
    test_refs: list[str] = []
    for item in list(fail_nodeids or []) + list(related_tests or []):
        s = str(item).strip().replace("\\", "/")
        if s and s not in test_refs:
            test_refs.append(s)

    prefer_dirs: list[str] = []
    for s in seeds:
        rel = normalize_repo_path(s.file_path or "", root)
        if rel:
            parent = str(Path(rel).parent).replace("\\", "/")
            if parent and parent not in prefer_dirs and parent != ".":
                prefer_dirs.append(parent)

    expanded: list[SuspectLocation] = []
    seen_keys: set[tuple[str, str]] = set()

    def push(s: SuspectLocation) -> None:
        rel = normalize_repo_path(s.file_path or "", root)
        if not rel or _is_test_path(rel):
            return
        start = int(s.start_line or 1)
        key = (rel, s.function_name or "", start)
        if key in seen_keys:
            return
        # 同文件无函数名的粗粒度命中跳过；调用方扩展按行保留
        if (
            rel in existing_files
            and not s.function_name
            and s.reason != "调用方扩展"
        ):
            return
        seen_keys.add(key)
        expanded.append(
            SuspectLocation(
                file_path=rel,
                start_line=start,
                end_line=max(start, int(s.end_line or 1)),
                function_name=s.function_name,
                class_name=s.class_name,
                reason=s.reason or "语义扩展",
                confidence=float(s.confidence or 0.65),
            )
        )

    # Hop 1: 测试文件 → imports / calls → definitions
    for ref in test_refs[:4]:
        file_part = ref.split("::", 1)[0]
        func_part = ""
        if "::" in ref:
            func_part = ref.split("::")[-1].split("[", 1)[0]
        rel = normalize_repo_path(file_part, root)
        if not rel:
            continue
        path = root / rel
        if not path.is_file():
            continue
        names, imports = symbols_from_python_file(path, focus_func=func_part)
        for mod, imported in imports[:12]:
            for cand in _module_to_candidates(root, mod):
                try:
                    crel = str(cand.relative_to(root)).replace("\\", "/")
                except ValueError:
                    continue
                # 在模块内定位 imported 名
                hits = find_definitions(
                    root, imported.split(".")[0], prefer_dirs=[str(Path(crel).parent)], max_hits=1
                )
                if hits:
                    hpath, line, kind = hits[0]
                    push(
                        SuspectLocation(
                            file_path=hpath,
                            start_line=line,
                            end_line=line,
                            function_name=imported.split(".")[0] if kind != "class" else None,
                            class_name=imported.split(".")[0] if kind == "class" else None,
                            reason="测试导入",
                            confidence=0.78,
                        )
                    )
                else:
                    push(
                        SuspectLocation(
                            file_path=crel,
                            start_line=1,
                            end_line=1,
                            reason="测试导入模块",
                            confidence=0.7,
                        )
                    )
                if len(expanded) >= max_new:
                    return expanded
        for name in names:
            for hpath, line, kind in find_definitions(
                root, name, prefer_dirs=prefer_dirs, max_hits=2
            ):
                push(
                    SuspectLocation(
                        file_path=hpath,
                        start_line=line,
                        end_line=line,
                        function_name=name if kind != "class" else None,
                        class_name=name if kind == "class" else None,
                        reason="语义扩展",
                        confidence=0.72,
                    )
                )
                if len(expanded) >= max_new:
                    return expanded

    # Hop 2: issue 符号 → 定义
    for name in extract_symbols_from_issue(issue, limit=10):
        for hpath, line, kind in find_definitions(
            root, name, prefer_dirs=prefer_dirs, max_hits=1
        ):
            push(
                SuspectLocation(
                    file_path=hpath,
                    start_line=line,
                    end_line=line,
                    function_name=name if kind != "class" else None,
                    class_name=name if kind == "class" else None,
                    reason="issue 符号",
                    confidence=0.68,
                )
            )
            if len(expanded) >= max_new:
                return expanded

    # Hop 3: 已知嫌疑函数的调用方
    pref_paths = [root / d for d in prefer_dirs]
    for s in seeds[:5]:
        func = s.function_name or ""
        if not func:
            continue
        for caller in _find_callers(
            root,
            func,
            prefer_dirs=pref_paths,
            exclude_files=existing_files,
            max_hits=2,
        ):
            push(caller)
            if len(expanded) >= max_new:
                return expanded

    return expanded[:max_new]
