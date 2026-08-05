"""Repo 内路径解析：缺包前缀 / 相对残片 → 唯一文件。"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "is_impl_py_path",
    "normalize_rel_path",
    "resolve_repo_file",
    "resolve_repo_relpath",
]


def normalize_rel_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./")


def is_impl_py_path(rel: str) -> bool:
    """非测试的 .py 实现路径（启发式）。"""
    p = normalize_rel_path(rel)
    if not p.endswith(".py"):
        return False
    base = Path(p).name
    if base.startswith("test_") or base.endswith("_test.py"):
        return False
    lowered = f"/{p.lower()}/"
    if any(h in lowered for h in ("/tests/", "/test/", "/testing/")):
        return False
    return True


def resolve_repo_file(repo_root: str | Path, file_path: str) -> Path | None:
    """将 patch 路径解析到 repo 内真实文件；支持后缀唯一匹配。"""
    if not file_path:
        return None
    root = Path(repo_root).resolve()
    raw = Path(file_path)
    if raw.is_absolute():
        try:
            cand = raw.resolve()
            cand.relative_to(root)
        except (ValueError, OSError):
            return None
        return cand if cand.is_file() else None

    direct = (root / file_path).resolve()
    try:
        direct.relative_to(root)
    except ValueError:
        return None
    if direct.is_file():
        return direct

    return _resolve_by_suffix(root, normalize_rel_path(file_path))


def resolve_repo_relpath(repo_root: str | Path, file_path: str) -> str | None:
    path = resolve_repo_file(repo_root, file_path)
    if path is None:
        return None
    root = Path(repo_root).resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _resolve_by_suffix(root: Path, norm: str) -> Path | None:
    if not norm or ".." in norm.split("/"):
        return None
    name = Path(norm).name
    if not name:
        return None
    matches: list[Path] = []
    try:
        for p in root.rglob(name):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel == norm or rel.endswith("/" + norm):
                matches.append(p)
    except OSError:
        return None
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # 更长后缀优先（更具体）
        matches.sort(key=lambda p: len(p.as_posix()), reverse=True)
        # 若前两名同样以 norm 结尾且路径不同，仍取唯一最短相对路径匹配
        exact = [p for p in matches if p.relative_to(root).as_posix().endswith(norm)]
        if len(exact) == 1:
            return exact[0]
    return None
