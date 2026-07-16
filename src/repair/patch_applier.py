"""Patch parse/apply service extracted from Orchestrator."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from src.state import CandidatePatch


def apply_patch_to_text(text: str, patch: CandidatePatch) -> str | None:
    """将 CandidatePatch 应用到文件文本，支持 original_lines 或 unified diff。"""
    if patch.original_lines and patch.patched_lines:
        if patch.original_lines in text:
            return text.replace(patch.original_lines, patch.patched_lines, 1)
        replaced = _replace_line_by_strip(text, patch.original_lines, patch.patched_lines)
        if replaced is not None:
            return replaced

    if patch.diff:
        result = _apply_unified_diff(text, patch.diff, file_path=patch.file_path)
        if result is not None:
            return result
        return _apply_import_line_fallback(text, patch.diff)

    return None


def _sync_import_symbol_usages(old_text: str, new_text: str, patch: CandidatePatch) -> str:
    """import 符号重命名后，同步替换文件内对旧符号的调用。"""
    rename = _infer_import_symbol_rename(old_text, new_text, patch)
    if not rename:
        return new_text
    old_sym, new_sym = rename
    return re.sub(rf"\b{re.escape(old_sym)}\s*\(", f"{new_sym}(", new_text)


def _infer_import_symbol_rename(
    old_text: str, new_text: str, patch: CandidatePatch
) -> tuple[str, str] | None:
    """从 import 行变更推断符号重命名（hello → greet）。"""
    candidates: list[tuple[str, str]] = []
    if patch.original_lines and patch.patched_lines:
        pair = _extract_import_symbol_pair(patch.original_lines, patch.patched_lines)
        if pair:
            candidates.append(pair)
    minus, plus = _extract_diff_line_pairs(patch.diff or "")
    if minus and plus:
        pair = _extract_import_symbol_pair(minus[0], plus[0])
        if pair:
            candidates.append(pair)
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    for old_sym, new_sym in candidates:
        if old_sym == new_sym:
            continue
        for old_line, new_line in zip(old_lines, new_lines):
            if old_line == new_line:
                continue
            if not _is_import_line(old_line) or not _is_import_line(new_line):
                continue
            if old_sym in old_line and new_sym in new_line:
                return old_sym, new_sym
    return None


def _extract_import_symbol_pair(old_line: str, new_line: str) -> tuple[str, str] | None:
    old_m = re.search(r"import\s+(\w+)\s*(?:#|$)", old_line)
    new_m = re.search(r"import\s+(\w+)\s*(?:#|$)", new_line)
    if old_m and new_m and old_m.group(1) != new_m.group(1):
        return old_m.group(1), new_m.group(1)
    return None


def _is_import_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("from ", "import "))


def _extract_diff_line_pairs(diff: str) -> tuple[list[str], list[str]]:
    minus: list[str] = []
    plus: list[str] = []
    for line in diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            minus.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            plus.append(line[1:])
    return minus, plus


def _apply_import_line_fallback(text: str, diff: str) -> str | None:
    """import 行补丁匹配失败时，按 diff 中的模块路径替换对应 import 行。"""
    minus, plus = _extract_diff_line_pairs(diff)
    if len(minus) != 1 or len(plus) != 1:
        return None
    old_line, new_line = minus[0], plus[0]
    if not (_is_import_line(old_line) or _is_import_line(plus[0])):
        return None

    old_key = _line_match_key(old_line)
    if old_key:
        replaced = _replace_line_by_strip(text, old_line, new_line)
        if replaced is not None:
            return replaced

    old_module = _extract_import_module(old_line)
    new_module = _extract_import_module(new_line)
    if not new_module:
        return None

    lines = text.splitlines(keepends=True)
    for i, file_line in enumerate(lines):
        content = file_line.rstrip("\n\r")
        if not _is_import_line(content):
            continue
        file_module = _extract_import_module(content)
        should_replace = False
        if old_module and (
            old_module in content or _import_modules_related(file_module, old_module)
        ):
            should_replace = True
        elif (
            file_module
            and file_module != new_module
            and _import_modules_related(file_module, new_module)
        ):
            should_replace = True
        if not should_replace:
            continue
        indent = content[: len(content) - len(content.lstrip())]
        if file_module and file_module != new_module and file_module in content:
            replacement = content.replace(file_module, new_module, 1)
        else:
            replacement = new_line.strip()
            if indent and not replacement.startswith((" ", "\t")):
                replacement = indent + replacement
        ending = file_line[len(content) :] if file_line.endswith(("\n", "\r")) else "\n"
        lines[i] = replacement + ending
        return "".join(lines)
    return None


def _import_modules_related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _extract_import_module(line: str) -> str:
    stripped = _line_match_key(line)
    m = re.match(r"from\s+([\w.]+)\s+import", stripped)
    if m:
        return m.group(1)
    m = re.match(r"import\s+([\w.]+)", stripped)
    if m:
        return m.group(1)
    return ""


def _line_match_key(line: str) -> str:
    """比较行内容时忽略注释与首尾空白。"""
    return line.split("#", 1)[0].strip()


def _replace_line_by_strip(text: str, old_line: str, new_line: str) -> str | None:
    """按 strip 后的内容匹配单行并替换，保留原缩进。"""
    old_key = _line_match_key(old_line)
    if not old_key:
        return None

    lines = text.splitlines(keepends=True)
    for i, file_line in enumerate(lines):
        content = file_line.rstrip("\n\r")
        if _line_match_key(content) != old_key:
            continue
        indent = content[: len(content) - len(content.lstrip())]
        replacement = new_line.strip()
        if indent and not replacement.startswith((" ", "\t")):
            replacement = indent + replacement
        ending = file_line[len(content) :] if file_line.endswith(("\n", "\r")) else "\n"
        lines[i] = replacement + ending
        return "".join(lines)
    return None


def _statement_anchor(line: str) -> str:
    stripped = _line_match_key(line)
    if stripped.startswith("return "):
        return "return "
    assign = re.match(r"([A-Za-z_][\w.]*\s*=)", stripped)
    return assign.group(1) if assign else ""


def _replace_unique_statement_by_anchor(
    text: str, old_line: str, new_line: str
) -> str | None:
    """old_line 表达式不精确时，按唯一语句锚点保守替换。"""
    old_anchor = _statement_anchor(old_line)
    new_anchor = _statement_anchor(new_line)
    if not old_anchor:
        return None
    allow_multiline_return = old_anchor == "return " and "\n" in new_line.strip()
    if old_anchor != new_anchor and not allow_multiline_return:
        return None

    lines = text.splitlines(keepends=True)
    matches: list[int] = []
    for i, file_line in enumerate(lines):
        content = file_line.rstrip("\n\r")
        if content.lstrip().startswith(old_anchor):
            matches.append(i)
    if len(matches) != 1:
        return None

    i = matches[0]
    content = lines[i].rstrip("\n\r")
    indent = content[: len(content) - len(content.lstrip())]
    replacement_lines = []
    for raw in new_line.strip().split("\n"):
        replacement = raw.strip()
        if indent and replacement and not replacement.startswith((" ", "\t")):
            replacement = indent + replacement
        replacement_lines.append(replacement)
    replacement = "\n".join(replacement_lines)
    ending = lines[i][len(content) :] if lines[i].endswith(("\n", "\r")) else "\n"
    lines[i] = replacement + ending
    return "".join(lines)


def _normalize_diff_path(path: str) -> str:
    path = path.strip().strip('"').replace("\\", "/")
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _select_diff_for_file(diff: str, file_path: str) -> str:
    """若 diff 含多个文件，只保留当前 CandidatePatch 对应的文件段。"""
    target = _normalize_diff_path(file_path)
    if not target:
        return diff

    lines = diff.splitlines()
    sections: list[tuple[str, str, list[str]]] = []
    current_old = ""
    current_new = ""
    current_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("--- ") and next_line.startswith("+++ "):
            if current_lines:
                sections.append((current_old, current_new, current_lines))
            current_old = _normalize_diff_path(line[4:])
            current_new = _normalize_diff_path(next_line[4:])
            current_lines = [line, next_line]
            i += 2
            continue
        if current_lines:
            current_lines.append(line)
        i += 1
    if current_lines:
        sections.append((current_old, current_new, current_lines))

    if not sections:
        return diff

    for old_path, new_path, section_lines in sections:
        if target in {old_path, new_path}:
            return "\n".join(section_lines)
    return diff


def _parse_diff_hunks(diff: str) -> list[tuple[list[str], list[str]]]:
    hunks: list[tuple[list[str], list[str]]] = []
    minus: list[str] = []
    plus: list[str] = []

    def flush() -> None:
        nonlocal minus, plus
        if minus or plus:
            hunks.append((minus, plus))
        minus = []
        plus = []

    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            flush()
            continue
        if line.startswith("-"):
            minus.append(line[1:])
        elif line.startswith("+"):
            plus.append(line[1:])
    flush()
    return hunks


def _apply_diff_hunk(text: str, minus: list[str], plus: list[str]) -> str | None:
    if not minus:
        return None

    old_block = "\n".join(minus)
    new_block = "\n".join(plus)
    if old_block in text:
        return text.replace(old_block, new_block, 1)

    if len(minus) == 1 and plus:
        replaced = _replace_line_by_strip(text, minus[0], new_block)
        if replaced is not None:
            return replaced
        return _replace_unique_statement_by_anchor(text, minus[0], new_block)

    if len(minus) == len(plus):
        current = text
        for old_line, new_line in zip(minus, plus, strict=True):
            replaced = _replace_line_by_strip(current, old_line, new_line)
            if replaced is None:
                return None
            current = replaced
        return current

    return None


def _apply_unified_diff(text: str, diff: str, *, file_path: str = "") -> str | None:
    """应用简化的 unified diff（-/+ 行）。"""
    selected_diff = _select_diff_for_file(diff, file_path)
    minus: list[str] = []
    plus: list[str] = []
    for line in selected_diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            minus.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            plus.append(line[1:])

    if not minus:
        return None

    old_block = "\n".join(minus)
    new_block = "\n".join(plus)
    if old_block in text:
        return text.replace(old_block, new_block, 1)

    if len(minus) == 1 and plus:
        replaced = _replace_line_by_strip(text, minus[0], plus[0])
        if replaced is not None:
            return replaced

    hunks = _parse_diff_hunks(selected_diff)
    if len(hunks) > 1:
        current = text
        for hunk_minus, hunk_plus in hunks:
            replaced = _apply_diff_hunk(current, hunk_minus, hunk_plus)
            if replaced is None:
                return None
            current = replaced
        return current if current != text else None

    if len(hunks) == 1:
        return _apply_diff_hunk(text, hunks[0][0], hunks[0][1])

    return None


def extract_json_block(text: str) -> str:
    """从文本中提取 JSON 块（优先处理 markdown 代码块）。"""
    text = text.strip()

    # 1. 从 markdown ```json...``` 代码块提取（支持嵌套括号）
    md_start = re.search(r"```(?:json)?\s*", text)
    if md_start:
        content = text[md_start.end() :]
        md_end = content.rfind("```")
        if md_end >= 0:
            inner = content[:md_end].strip()
            if inner.startswith("{") or inner.startswith("["):
                return inner

    # 2. 尝试直接作为 JSON 解析
    if text.startswith("[") or text.startswith("{"):
        return text

    # 3. 搜索最近邻的完整 JSON 块（从最后一个 [ 或 { 开始）
    for start_char in ("[", "{"):
        end_char = "]" if start_char == "[" else "}"
        last_start = text.rfind(start_char)
        if last_start >= 0:
            # 从该位置找到配对的闭合
            depth = 0
            for i in range(last_start, len(text)):
                if text[i] == start_char:
                    depth += 1
                elif text[i] == end_char:
                    depth -= 1
                    if depth == 0:
                        return text[last_start : i + 1]

    return text


class PatchApplier:
    """Parse and apply candidate patches under a repo root."""

    def __init__(self, repo_root: str):
        self.repo_root = repo_root

    def resolve_repo_file(self, file_path: str) -> Path | None:
        if not file_path:
            return None
        path = Path(file_path)
        root = Path(self.repo_root).resolve()
        if path.is_absolute():
            try:
                path.resolve().relative_to(root)
            except ValueError:
                return None
        else:
            path = (root / path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return path

    def apply_patches(self, patches: list[CandidatePatch]) -> list[CandidatePatch]:
        applied: list[CandidatePatch] = []
        for p in patches:
            file_path = self.resolve_repo_file(p.file_path)
            if file_path is None:
                print(
                    f"  [patcher] ⚠ 拒绝补丁（路径不在 repo 或文件不存在）: {p.file_path!r}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            text = file_path.read_text(encoding="utf-8")
            new_text = apply_patch_to_text(text, p)
            if new_text is None:
                print(
                    f"  [patcher] ⚠ 无法应用补丁: {p.file_path}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            new_text = _sync_import_symbol_usages(text, new_text, p)
            file_path.write_text(new_text, encoding="utf-8")
            applied.append(p)
        return applied


def parse_patches(answer: str) -> list[CandidatePatch]:
    import json

    text = answer.strip()
    json_str = extract_json_block(text)
    if json_str:
        try:
            data = json.loads(json_str)
            if isinstance(data, list):
                return [CandidatePatch.from_dict(item) for item in data]
            if isinstance(data, dict) and "patches" in data:
                return [CandidatePatch.from_dict(item) for item in data["patches"]]
        except (json.JSONDecodeError, KeyError):
            pass
    for m in re.finditer(r"\[[\s\S]*?\{[\s\S]*?\}[\s\S]*?\]", text):
        try:
            data = json.loads(m.group())
            if isinstance(data, list) and len(data) > 0:
                return [CandidatePatch.from_dict(item) for item in data]
        except (json.JSONDecodeError, KeyError):
            continue
    return []
