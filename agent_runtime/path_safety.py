"""Workspace 路径安全：符号链接逃逸检测 + canonical 边界校验。"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["is_path_under_root", "resolve_under_root"]


def is_path_under_root(path: Path, root: Path) -> bool:
    """*path* 解析后是否位于 *root* 内（含 root 自身）。"""
    root_real = root.resolve()
    try:
        path.resolve().relative_to(root_real)
        return True
    except ValueError:
        return False


def _reject_escape(raw_path: str, *, via_symlink: bool = False, detail: str = "") -> None:
    if via_symlink:
        extra = f" → {detail}" if detail else ""
        raise ValueError(
            f"符号链接逃逸被拦截: {raw_path}（symlink 目标在 workspace 外{extra}）"
        )
    raise ValueError(f"路径逃逸被拦截: {raw_path}（禁止访问 workspace 外路径）")


def _resolve_symlink(link: Path) -> Path:
    target = link.readlink()
    if not target.is_absolute():
        target = link.parent / target
    return target.resolve()


def resolve_under_root(root: Path | str, raw_path: str) -> Path:
    """将 *raw_path* 解析为位于 *root* 内的 canonical 绝对路径。

    逐步遍历路径分量；遇到 symlink 立即校验目标仍在 root 内；
    最终再 ``resolve()`` 二次校验。
    """
    root_real = Path(root).resolve()
    text = (raw_path or "").strip()
    if not text:
        raise ValueError("无法解析路径: 空路径")

    raw = Path(text)
    if raw.is_absolute():
        try:
            resolved = raw.resolve()
        except OSError as exc:
            raise ValueError(f"无法解析路径: {raw_path}") from exc
        if not is_path_under_root(resolved, root_real):
            _reject_escape(raw_path)
        return resolved

    current = root_real
    for part in raw.parts:
        if part in (".", ""):
            continue
        if part == "..":
            parent = current.parent
            if not is_path_under_root(parent, root_real):
                _reject_escape(raw_path)
            current = parent
            continue

        current = current / part
        if current.is_symlink():
            try:
                resolved_target = _resolve_symlink(current)
            except OSError as exc:
                raise ValueError(f"无法解析符号链接: {raw_path}") from exc
            if not is_path_under_root(resolved_target, root_real):
                _reject_escape(
                    raw_path,
                    via_symlink=True,
                    detail=str(resolved_target),
                )
            current = resolved_target

    try:
        final = current.resolve()
    except OSError as exc:
        raise ValueError(f"无法解析路径: {raw_path}") from exc

    if not is_path_under_root(final, root_real):
        try:
            common = os.path.commonpath([str(root_real), str(final)])
        except ValueError:
            raise ValueError(f"无法解析路径: {raw_path}")
        if common != str(root_real):
            _reject_escape(raw_path)

    return final
