"""SWE / benchmark：verify 前应用 test_patch，结束后还原（不进 model 导出）。"""

from __future__ import annotations

import re
from pathlib import Path

from src.eval.patch_utils import apply_unified_patch

__all__ = [
    "VerifyTestPatchOverlay",
    "apply_test_patch_safe",
    "extract_targets_from_test_patch",
    "iter_test_patch_paths",
]


def iter_test_patch_paths(patch_text: str) -> list[str]:
    """提取 unified diff 中的目标相对路径（``+++ b/...``）。"""
    paths: list[str] = []
    seen: set[str] = set()
    for line in (patch_text or "").splitlines():
        if line.startswith("+++ b/"):
            rel = line[6:].strip().replace("\\", "/")
            if rel and rel != "/dev/null" and rel not in seen:
                seen.add(rel)
                paths.append(rel)
    return paths


def extract_targets_from_test_patch(patch_text: str) -> list[str]:
    """从 test_patch 推断可试的 pytest target（文件 + 新增 test 名）。"""
    if not (patch_text or "").strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    files = iter_test_patch_paths(patch_text)
    for rel in files:
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    # 新增的 def test_*（+ 行）
    for m in re.finditer(r"^\+\s*def\s+(test_\w+)\s*\(", patch_text or "", re.MULTILINE):
        name = m.group(1)
        for rel in files:
            if rel.endswith(".py"):
                node = f"{rel}::{name}"
                if node not in seen:
                    seen.add(node)
                    out.append(node)
                break
        else:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def apply_test_patch_safe(repo: Path, patch_text: str) -> None:
    """应用 test_patch；支持新建文件（官方 test_patch 常见）。"""
    text = (patch_text or "").strip()
    if not text:
        return
    repo = Path(repo)
    # 预创建缺失文件，避免 apply_unified_patch 读失败
    for rel in iter_test_patch_paths(text):
        path = repo / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    apply_unified_patch(repo, text)


class VerifyTestPatchOverlay:
    """在 repo 上临时打 test_patch，退出时按文件还原。

    用于 FixLoop verify：对齐 SWE「base + test_patch + model」收集用例，
    且不把官方测试变更留在工作树（避免进 model_patch 导出）。
    """

    def __init__(self, repo_root: str | Path, patch_text: str):
        self.repo = Path(repo_root)
        self.patch_text = patch_text or ""
        self._backup: dict[str, str | None] = {}
        self.applied = False

    def __enter__(self) -> VerifyTestPatchOverlay:
        if not self.patch_text.strip():
            return self
        for rel in iter_test_patch_paths(self.patch_text):
            path = self.repo / rel
            if path.is_file():
                self._backup[rel] = path.read_text(encoding="utf-8", errors="replace")
            else:
                self._backup[rel] = None
        try:
            apply_test_patch_safe(self.repo, self.patch_text)
            self.applied = True
        except Exception:
            self._restore()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._restore()

    def _restore(self) -> None:
        if not self._backup and not self.applied:
            return
        for rel, content in self._backup.items():
            path = self.repo / rel
            if content is None:
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        self._backup.clear()
        self.applied = False
