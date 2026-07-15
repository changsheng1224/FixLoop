"""L1 补丁解析、预览与多 hunk 应用（不依赖 Layer 2）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PatchHunk:
    """单个 unified diff hunk。"""

    header: str
    lines: list[tuple[str, str]]  # (' ', '-', '+'), text without prefix


@dataclass
class PatchPlan:
    """待应用的补丁计划。"""

    mode: str  # "legacy" | "diff"
    hunks: list[PatchHunk] = field(default_factory=list)
    old_text: str = ""
    new_text: str = ""


@dataclass
class PatchPreview:
    path: str
    hunk_count: int
    lines_added: int
    lines_removed: int
    hunks: list[dict]


def parse_patch_input(args: dict) -> PatchPlan:
    """从 patch_file 参数解析补丁计划。"""
    diff = (args.get("diff") or "").strip()
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")

    if diff:
        hunks = _parse_unified_diff_hunks(diff)
        if not hunks:
            raise ValueError("diff 中未找到有效的 @@ hunk")
        return PatchPlan(mode="diff", hunks=hunks)

    if old_text and new_text is not None:
        return PatchPlan(mode="legacy", old_text=old_text, new_text=new_text)

    raise ValueError("必须提供 diff，或同时提供 old_text 与 new_text")


def build_preview(path: str, plan: PatchPlan) -> PatchPreview:
    """从补丁计划构建预览摘要。"""
    hunks_out: list[dict] = []
    lines_added = 0
    lines_removed = 0

    if plan.mode == "legacy":
        removed = plan.old_text.splitlines()
        added = plan.new_text.splitlines()
        lines_removed = len(removed)
        lines_added = len(added)
        hunks_out.append(
            {
                "header": "@@ legacy @@",
                "removed": removed[:20],
                "added": added[:20],
            }
        )
        return PatchPreview(
            path=path,
            hunk_count=1,
            lines_added=lines_added,
            lines_removed=lines_removed,
            hunks=hunks_out,
        )

    for hunk in plan.hunks:
        removed = [text for kind, text in hunk.lines if kind == "-"]
        added = [text for kind, text in hunk.lines if kind == "+"]
        lines_removed += len(removed)
        lines_added += len(added)
        hunks_out.append(
            {
                "header": hunk.header,
                "removed": removed[:20],
                "added": added[:20],
            }
        )

    return PatchPreview(
        path=path,
        hunk_count=len(plan.hunks),
        lines_added=lines_added,
        lines_removed=lines_removed,
        hunks=hunks_out,
    )


def apply_plan(text: str, plan: PatchPlan) -> str | None:
    """将补丁应用到文本；失败返回 None。"""
    if plan.mode == "legacy":
        count = text.count(plan.old_text)
        if count != 1:
            return None
        return text.replace(plan.old_text, plan.new_text, 1)

    lines = text.splitlines(keepends=True)
    if text and not text.endswith(("\n", "\r")):
        # 无尾换行时 splitlines(keepends=True) 最后一行无换行，与 hunk 对齐
        pass
    updated = _apply_hunks(lines, plan.hunks)
    return "".join(updated)


def format_preview_text(
    preview: PatchPreview,
    *,
    max_hunks: int = 3,
    max_lines: int = 8,
) -> str:
    """格式化为审批/UI 可读的 diff 摘要。"""
    header = (
        f"--- 预览 ({preview.hunk_count} hunk"
        f"{'' if preview.hunk_count == 1 else 's'}, "
        f"-{preview.lines_removed}/+{preview.lines_added} lines) ---"
    )
    parts = [header]
    for hunk in preview.hunks[:max_hunks]:
        parts.append(hunk["header"])
        shown = 0
        for line in hunk.get("removed", []):
            if shown >= max_lines:
                break
            parts.append(f"-{line}")
            shown += 1
        shown = 0
        for line in hunk.get("added", []):
            if shown >= max_lines:
                break
            parts.append(f"+{line}")
            shown += 1
    if preview.hunk_count > max_hunks:
        parts.append(f"... 另有 {preview.hunk_count - max_hunks} 个 hunk")
    return "\n".join(parts)


def preview_to_metadata(preview: PatchPreview) -> dict:
    """转为 trace / ToolExecutionResult metadata。"""
    return {
        "path": preview.path,
        "hunk_count": preview.hunk_count,
        "lines_added": preview.lines_added,
        "lines_removed": preview.lines_removed,
        "preview_text": format_preview_text(preview),
        "hunks": preview.hunks,
    }


def try_build_patch_preview(
    path: str, file_text: str, args: dict
) -> tuple[dict | None, str | None]:
    """解析参数并构建 preview metadata；失败返回 (None, error_msg)。"""
    try:
        plan = parse_patch_input(args)
    except ValueError as e:
        return None, str(e)

    if plan.mode == "legacy":
        count = file_text.count(plan.old_text)
        if count == 0:
            return None, "old_text 在文件中未找到（出现 0 次）"
        if count > 1:
            return None, f"old_text 出现 {count} 次，必须恰好出现 1 次"

    elif plan.mode == "diff":
        trial = apply_plan(file_text, plan)
        if trial is None:
            return None, "diff 无法应用到当前文件内容"

    preview = build_preview(path, plan)
    return preview_to_metadata(preview), None


def _parse_unified_diff_hunks(diff: str) -> list[PatchHunk]:
    """从 unified diff 文本提取 hunk 列表（忽略 ---/+++ 文件头）。"""
    lines = diff.strip().splitlines()
    hunks: list[PatchHunk] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@"):
            header = line
            i += 1
            hunk_lines: list[tuple[str, str]] = []
            while i < len(lines) and not lines[i].startswith("@@"):
                hl = lines[i]
                if hl.startswith(" ") or hl == "":
                    hunk_lines.append((" ", hl[1:] if hl.startswith(" ") else hl))
                elif hl.startswith("-") and not hl.startswith("---"):
                    hunk_lines.append(("-", hl[1:]))
                elif hl.startswith("+") and not hl.startswith("+++"):
                    hunk_lines.append(("+", hl[1:]))
                i += 1
            hunks.append(PatchHunk(header=header, lines=hunk_lines))
        else:
            i += 1
    return hunks


def _apply_hunks(original: list[str], hunks: list[PatchHunk]) -> list[str] | None:
    """按 unified diff 语义顺序应用多个 hunk。"""
    lines = original[:]
    for hunk in hunks:
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", hunk.header)
        if not m:
            return None
        old_start = int(m.group(1)) - 1
        old_idx = old_start
        new_segment: list[str] = []
        for kind, text in hunk.lines:
            if kind == " ":
                if old_idx >= len(lines):
                    return None
                new_segment.append(lines[old_idx])
                old_idx += 1
            elif kind == "-":
                if old_idx >= len(lines):
                    return None
                old_idx += 1
            elif kind == "+":
                chunk = text if text.endswith("\n") else text + "\n"
                new_segment.append(chunk)
        lines[old_start:old_idx] = new_segment
    return lines
