"""Codex 风格 apply_patch 文本解析（lenient）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ApplyPatchOp",
    "parse_apply_patch_text",
    "strip_fences",
    "update_diff_has_preimage",
]


@dataclass
class ApplyPatchOp:
    action: str  # update | add | delete
    path: str
    diff: str = ""


_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n([\s\S]*?)\n```\s*$", re.MULTILINE)


def update_diff_has_preimage(diff: str) -> bool:
    """Update hunk 须含删除行或上下文行，禁止仅有 + 的空 preimage。"""
    for ln in (diff or "").splitlines():
        if ln.startswith("---") or ln.startswith("+++"):
            continue
        if ln.startswith("-"):
            return True
        if ln.startswith(" ") and ln.strip() != "":
            return True
    return False


# 兼容内部旧名
_update_diff_has_preimage = update_diff_has_preimage


def strip_fences(text: str) -> str:
    """剥 markdown / heredoc 围栏，便于模型 freeform 输出。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    # 整段围栏
    m = _FENCE_RE.match(raw)
    if m:
        return m.group(1).strip()
    # 去首尾 ``` 行
    lines = raw.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    # heredoc 风格
    if lines and lines[0].strip().startswith("<<"):
        lines = lines[1:]
        if lines and lines[-1].strip() in (".", "EOF", "PATCH"):
            lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_apply_patch_text(text: str) -> list[ApplyPatchOp]:
    """解析 *** Begin/End Patch 块；失败抛 ValueError（语法级）。"""
    body = strip_fences(text)
    if not body:
        raise ValueError("apply_patch: empty input")

    # 无 Begin 标记时：若像 unified diff 且带 --- a/ 则拒（需 path）；允许裸 Update
    if "*** Begin Patch" not in body and "*** Update File:" not in body:
        if body.lstrip().startswith("@@") or "--- " in body[:80]:
            raise ValueError(
                "apply_patch: missing *** Begin Patch / *** Update File: path header"
            )
        raise ValueError("apply_patch: expected *** Begin Patch ... *** End Patch")

    # 取 Begin..End 之间；若无 End 则用全文
    start = body.find("*** Begin Patch")
    if start >= 0:
        body = body[start + len("*** Begin Patch") :]
    end = body.find("*** End Patch")
    if end >= 0:
        body = body[:end]
    body = body.strip()

    ops: list[ApplyPatchOp] = []
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("*** Update File:"):
            path = line.split(":", 1)[1].strip().replace("\\", "/")
            i += 1
            diff_lines: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("*** "):
                diff_lines.append(lines[i])
                i += 1
            diff_body = "\n".join(diff_lines).strip()
            if not diff_body:
                raise ValueError(
                    f"apply_patch: empty Update File body for {path} "
                    "(need @@ hunk with -/+ context; read file first)"
                )
            if not _update_diff_has_preimage(diff_body):
                raise ValueError(
                    f"apply_patch: Update File {path} missing preimage "
                    "(- or context lines); refuse empty_original"
                )
            ops.append(ApplyPatchOp(action="update", path=path, diff=diff_body))
            continue
        if line.startswith("*** Add File:"):
            path = line.split(":", 1)[1].strip().replace("\\", "/")
            i += 1
            content_lines: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("*** "):
                raw_l = lines[i]
                if raw_l.startswith("+"):
                    content_lines.append(raw_l[1:])
                else:
                    content_lines.append(raw_l)
                i += 1
            ops.append(
                ApplyPatchOp(
                    action="add",
                    path=path,
                    diff="\n".join(content_lines),
                )
            )
            continue
        if line.startswith("*** Delete File:"):
            path = line.split(":", 1)[1].strip().replace("\\", "/")
            ops.append(ApplyPatchOp(action="delete", path=path))
            i += 1
            continue
        i += 1

    if not ops:
        raise ValueError("apply_patch: no Update/Add/Delete File ops found")
    return ops
