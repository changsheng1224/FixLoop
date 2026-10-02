"""Resolve public test references to unambiguous, repository-contained pytest targets."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from agent_runtime.path_safety import resolve_under_root

_UNITTEST_STYLE = re.compile(
    r"^(?P<method>[A-Za-z_]\w*)\s+"
    r"\((?P<qual>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\)\s*$"
)
_SKIP_DIRS = frozenset(
    {".git", ".agent", ".fixloop", ".venv", "venv", "node_modules", "__pycache__", "site-packages"}
)


class TestReferenceResolver:
    """Share a lazy file inventory across references; ambiguous matches remain unresolved."""

    def __init__(self, repo_root: str | Path | None = None):
        self.root = Path(repo_root).resolve() if repo_root else None
        self._files: list[Path] | None = None
        self._definitions: dict[str, list[str]] | None = None

    def _repo_files(self) -> list[Path]:
        if self._files is None:
            self._files = []
            if self.root is not None:
                for directory, dirs, files in os.walk(self.root, followlinks=False):
                    dirs[:] = sorted(
                        d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
                    )
                    for name in sorted(files):
                        if name.endswith(".py"):
                            path = Path(directory) / name
                            if self._contained_file(path) is not None:
                                self._files.append(path)
        return self._files

    def _contained_file(self, raw: str | Path, *, allow_directory: bool = False) -> str | None:
        if self.root is None:
            return None
        try:
            path = resolve_under_root(self.root, str(raw))
            if path.is_file() or (allow_directory and path.is_dir()):
                return path.relative_to(self.root).as_posix()
        except (OSError, ValueError):
            pass
        return None

    def _file(self, raw: str) -> str | None:
        if self.root is None:
            return None
        try:
            resolve_under_root(self.root, raw)
        except (OSError, ValueError):
            return None
        direct = self._contained_file(raw)
        if direct:
            return direct
        # Escaping paths must never be reinterpreted as another file inside the repo.
        if Path(raw).is_absolute() or ".." in Path(raw).parts:
            return None
        parts = Path(raw).parts
        matches = [p for p in self._repo_files() if p.name == Path(raw).name]
        for count in range(len(parts), 0, -1):
            suffix = parts[-count:]
            selected = [p for p in matches if p.parts[-count:] == suffix]
            if len(selected) == 1:
                return self._contained_file(selected[0])
            if len(selected) > 1:
                return None
        return None

    def _definition_targets(self, name: str) -> list[str]:
        if self._definitions is None:
            self._definitions = {}
            for path in self._repo_files():
                if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError, SyntaxError):
                    continue
                rel = self._contained_file(path)
                if not rel:
                    continue
                for node in tree.body:
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                        self._definitions.setdefault(node.name, []).append(f"{rel}::{node.name}")
                    elif isinstance(node, ast.ClassDef):
                        is_test_class = node.name.startswith("Test") or any(
                            (isinstance(base, ast.Name) and base.id == "TestCase")
                            or (isinstance(base, ast.Attribute) and base.attr == "TestCase")
                            for base in node.bases
                        )
                        if is_test_class:
                            for method in node.body:
                                if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                                    self._definitions.setdefault(method.name, []).append(
                                        f"{rel}::{node.name}::{method.name}"
                                    )
        return self._definitions.get(name, [])

    def resolve(self, ref: str) -> str:
        raw = (ref or "").strip().replace("\\", "/")
        if not raw:
            return ""
        match = _UNITTEST_STYLE.fullmatch(raw)
        if match:
            parts = match["qual"].split(".")
            class_name = parts.pop() if parts[-1][0].isupper() else ""
            module = "/".join(parts) + ".py"
            selectors = [x for x in (class_name, match["method"]) if x]
            if self.root is None:
                return "::".join([module, *selectors])
            for prefix in ("", "tests/", "test/"):
                found = self._contained_file(prefix + module)
                if found:
                    return "::".join([found, *selectors])
            found = self._file(module)
            return "::".join([found, *selectors]) if found else ""
        if self.root is None:
            return raw
        file_part, sep, selectors = raw.partition("::")
        direct = self._contained_file(file_part, allow_directory=not sep)
        if direct:
            return direct + (sep + selectors if selectors else "")
        if raw.isidentifier():
            if not raw.startswith("test_"):
                return ""
            targets = self._definition_targets(raw)
            return targets[0] if len(targets) == 1 else ""
        found = self._file(file_part)
        return found + (sep + selectors if selectors else "") if found else ""

    def normalize(self, refs: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                target for ref in refs if isinstance(ref, str) and (target := self.resolve(ref))
            )
        )


def resolve_test_ref_for_pytest(ref: str, repo_root: str | Path | None = None) -> str:
    return TestReferenceResolver(repo_root).resolve(ref)


def normalize_related_test_refs(refs: list[str], repo_root: str | Path | None = None) -> list[str]:
    return TestReferenceResolver(repo_root).normalize(refs)
