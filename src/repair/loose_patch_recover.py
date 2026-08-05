"""从非 JSON 模型输出回收 unified diff → CandidatePatch。"""

from __future__ import annotations

import re

from src.state import CandidatePatch

__all__ = [
    "parse_patches_with_recover",
    "recover_patches_from_text",
]

_FENCE_RE = re.compile(
    r"```(?:diff|patch|udiff)?\s*\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_DIFF_HEADER_RE = re.compile(
    r"(?m)^(?:diff --git a/.+ b/.+\n)?---[ \t]+(?:a/)?(.+?)\n\+\+\+[ \t]+(?:b/)?(.+?)(?:\n|$)"
)


def _strip_path(raw: str) -> str:
    p = (raw or "").strip().strip('"').strip("'")
    if p == "/dev/null":
        return ""
    # drop timestamps: "file.py\t2020-01-01"
    p = p.split("\t", 1)[0].strip()
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def _split_unified_blobs(text: str) -> list[str]:
    """按 diff --git / --- a/ 边界切分多个文件 diff。"""
    raw = text.strip()
    if not raw:
        return []
    # Prefer diff --git boundaries
    parts = re.split(r"(?m)(?=^diff --git )", raw)
    blobs = [p.strip() for p in parts if p.strip()]
    if len(blobs) > 1 or (blobs and blobs[0].startswith("diff --git")):
        return blobs
    # Fallback: --- a/ boundaries
    parts = re.split(r"(?m)(?=^---[ \t])", raw)
    return [p.strip() for p in parts if p.strip() and p.lstrip().startswith("---")]


def _patch_from_blob(blob: str) -> CandidatePatch | None:
    m = _DIFF_HEADER_RE.search(blob)
    if not m:
        return None
    old_p = _strip_path(m.group(1))
    new_p = _strip_path(m.group(2))
    file_path = new_p or old_p
    if not file_path:
        return None
    # Ensure blob starts at --- for applier friendliness
    start = m.start()
    diff_body = blob[start:].strip()
    if not diff_body.endswith("\n"):
        diff_body += "\n"
    # Prepend diff --git if missing (optional but nicer)
    if not diff_body.startswith("diff --git"):
        diff_body = f"diff --git a/{file_path} b/{file_path}\n{diff_body}"
    return CandidatePatch(
        file_path=file_path,
        diff=diff_body,
        explanation="loose_diff_recover",
    )


def recover_patches_from_text(answer: str) -> list[CandidatePatch]:
    """从 markdown fence 或裸 unified diff 提取 CandidatePatch。"""
    text = (answer or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    for m in _FENCE_RE.finditer(text):
        body = (m.group(1) or "").strip()
        if "---" in body and "+++" in body:
            chunks.append(body)
    if not chunks and "---" in text and "+++" in text:
        chunks.append(text)

    out: list[CandidatePatch] = []
    seen: set[str] = set()
    for chunk in chunks:
        for blob in _split_unified_blobs(chunk):
            patch = _patch_from_blob(blob)
            if patch is None:
                continue
            key = patch.file_path.replace("\\", "/")
            if key in seen:
                continue
            seen.add(key)
            out.append(patch)
    return out


def parse_patches_with_recover(answer: str) -> list[CandidatePatch]:
    """先 JSON parse_patches，失败再 loose recover。"""
    from src.repair.patch_applier import parse_patches

    patches = parse_patches(answer or "")
    if patches:
        return patches
    return recover_patches_from_text(answer or "")
