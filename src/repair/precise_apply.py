"""CandidatePatch → 与 patch_file 同源的精确落盘（优先于模糊匹配）。"""

from __future__ import annotations

from agent_runtime.patch_engine import apply_plan, parse_patch_input
from src.state import CandidatePatch


def apply_candidate_precise(text: str, patch: CandidatePatch) -> str | None:
    """精确 apply：原文子串全量替换（E6a）或 ``patch_engine`` unified diff。

    与 ``tool_patch_file`` 共用 ``apply_plan``；不做 strip/折叠空白等模糊匹配。
    调用方应先 ``normalize_patch_text_field`` 写回 patch 字段。
    """
    original = patch.original_lines or ""
    patched = patch.patched_lines or ""

    if original and original in text:
        # 精确子串：全部命中替换（sibling 同 pre-image）
        return text.replace(original, patched)

    diff = (patch.diff or "").strip()
    if diff and "@@" in diff:
        try:
            plan = parse_patch_input({"diff": diff})
        except ValueError:
            return None
        return apply_plan(text, plan)

    return None
