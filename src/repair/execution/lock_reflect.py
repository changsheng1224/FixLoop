"""F2P → 实现路径（仅种子护栏，不做强制反思剧本）。"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "f2p_impl_paths",
    "merge_f2p_paths_first",
    "fallback_impls_from_f2p_hints",
]


def fallback_impls_from_f2p_hints(
    hints: list[str], repo_root: str | Path, *, max_keep: int = 8
) -> list[str]:
    """无符号索引时：由测试路径启发式映射到同包实现 ``.py``。"""
    from src.repair.localization.localize_quality import _is_test_path

    root = Path(repo_root)
    out: list[str] = []
    seen: set[str] = set()
    for hint in hints or []:
        file_part = str(hint).replace("\\", "/").split("::", 1)[0].lstrip("./")
        if not file_part:
            continue
        parts = [p for p in file_part.split("/") if p]
        stem = parts[-1]
        if stem.endswith(".py"):
            stem = stem[:-3]
        if stem.startswith("test_"):
            cand_name = stem[len("test_") :] + ".py"
        elif stem.endswith("_test"):
            cand_name = stem[: -len("_test")] + ".py"
        else:
            cand_name = ""

        pkg_parts = list(parts[:-1])
        while pkg_parts and pkg_parts[-1] in ("tests", "test"):
            pkg_parts.pop()

        candidates: list[str] = []
        if cand_name and pkg_parts:
            candidates.append("/".join(pkg_parts + [cand_name]))
        if pkg_parts:
            pkg_dir = root.joinpath(*pkg_parts)
            if pkg_dir.is_dir():
                pys = sorted(
                    p.name
                    for p in pkg_dir.glob("*.py")
                    if p.is_file()
                    and not _is_test_path(
                        str(p.relative_to(root)).replace("\\", "/")
                    )
                )
                for name in pys[:5]:
                    candidates.append("/".join(pkg_parts + [name]))

        for rel in candidates:
            if rel in seen or _is_test_path(rel):
                continue
            if (root / rel).is_file():
                seen.add(rel)
                out.append(rel)
                if len(out) >= max_keep:
                    return out
    return out


def f2p_impl_paths(issue: str, repo_root: str | Path, *, max_keep: int = 8) -> list[str]:
    """从 issue 的 FAIL_TO_PASS 得到实现文件路径（去测试、去重）。"""
    from src.repair.localization.fail_to_pass_hints import extract_fail_to_pass_hints
    from src.repair.localization.localize_fastpath import suspects_from_fail_to_pass
    from src.repair.localization.localize_quality import _is_test_path

    out: list[str] = []
    seen: set[str] = set()
    for s in suspects_from_fail_to_pass(issue or "", repo_root, max_keep=max_keep):
        fp = (s.file_path or "").replace("\\", "/")
        if not fp or fp in seen or _is_test_path(fp):
            continue
        if not fp.endswith(".py"):
            continue
        seen.add(fp)
        out.append(fp)
        if len(out) >= max_keep:
            return out

    hints = extract_fail_to_pass_hints(issue or "")
    for fp in fallback_impls_from_f2p_hints(hints, repo_root, max_keep=max_keep):
        if fp in seen:
            continue
        seen.add(fp)
        out.append(fp)
        if len(out) >= max_keep:
            break
    return out


def merge_f2p_paths_first(
    f2p_impls: list[str],
    other: list[str],
    *,
    max_keep: int = 8,
) -> list[str]:
    """F2P 实现路径置顶，避免被噪声挤掉。"""
    seen: set[str] = set()
    out: list[str] = []
    for fp in list(f2p_impls or []) + list(other or []):
        n = (fp or "").replace("\\", "/")
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= max_keep:
            break
    return out
