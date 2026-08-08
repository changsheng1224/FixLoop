"""Patch parse/apply service extracted from Orchestrator."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from src.state import CandidatePatch


def normalize_patch_text_field(value: object) -> str:
    """将 patch 行字段规范为源码文本（E19）。

    根因：模型常把 ``original_lines``/``patched_lines`` 输出成 JSON list，
    或把 list 的 ``repr`` 当成字符串 → 导出出现 ``-['...']``，无法对齐源码。
    """
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if item is None:
                continue
            parts.append(str(item).rstrip("\r\n"))
        return "\n".join(parts)
    if isinstance(value, tuple):
        return normalize_patch_text_field(list(value))
    if not isinstance(value, str):
        return str(value)
    text = value
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == "[" and stripped[-1] == "]":
        try:
            import ast

            lit = ast.literal_eval(stripped)
            if isinstance(lit, list):
                return normalize_patch_text_field(lit)
        except (SyntaxError, ValueError, MemoryError):
            pass
    return text


def apply_patch_to_text(text: str, patch: CandidatePatch) -> str | None:
    """将 CandidatePatch 应用到文件文本，支持 original_lines 或 unified diff。"""
    from src.repair.execution.precise_apply import apply_candidate_precise

    original = normalize_patch_text_field(patch.original_lines)
    patched = normalize_patch_text_field(patch.patched_lines)
    # 写回规范化结果，供 sibling / 导出使用
    if original != (patch.original_lines or "") or patched != (patch.patched_lines or ""):
        patch.original_lines = original
        patch.patched_lines = patched

    # 1) 精确路径（与 patch_file / patch_engine 对齐）
    precise = apply_candidate_precise(text, patch)
    if precise is not None:
        return precise

    if original and patched:
        # 2) 有限模糊：strip / 多行键 / 折叠空白（精确失败后的兜底）
        replaced = _replace_all_lines_by_strip(text, original, patched)
        if replaced is not None:
            return replaced
        replaced = _replace_multiline_by_strip_keys(text, original, patched)
        if replaced is not None:
            return replaced
        replaced = _replace_by_collapsed_whitespace(text, original, patched)
        if replaced is not None:
            return replaced

    if patch.diff:
        result = _apply_unified_diff(text, patch.diff, file_path=patch.file_path)
        if result is not None:
            return result
        return _apply_import_line_fallback(text, patch.diff)

    return None


def describe_hunk_mismatch(text: str, patch: CandidatePatch) -> str:
    """生成可行动的 apply 失败描述（含源文件近邻行），供下一轮 patcher feedback。"""
    path = patch.file_path or "?"
    original = normalize_patch_text_field(patch.original_lines)
    if not original.strip():
        if patch.diff:
            return f"hunk_mismatch:{path}:diff_only_no_match"
        return f"hunk_mismatch:{path}:empty_original"

    first = next((ln for ln in original.splitlines() if ln.strip()), "")
    key = _line_match_key(first)
    collapsed = _collapse_ws(key)
    anchor = _statement_anchor(first)
    near: list[str] = []
    if key:
        token = collapsed[:48] if collapsed else key[:48]
        for i, ln in enumerate(text.splitlines(), 1):
            file_key = _line_match_key(ln)
            if not file_key:
                continue
            collapsed_file = _collapse_ws(file_key)
            hit = (
                key == file_key
                or (token and token in collapsed_file)
                or (len(key) >= 12 and key[:12] in file_key)
                or (anchor and file_key.lstrip().startswith(anchor))
            )
            if hit:
                near.append(f"L{i}:{ln.strip()[:140]}")
                if len(near) >= 3:
                    break

    preview = first.strip()[:100]
    if near:
        return (
            f"hunk_mismatch:{path}: wanted `{preview}` near=["
            + " | ".join(near)
            + "]"
        )
    return f"hunk_mismatch:{path}: wanted `{preview}` (no near lines in file)"


def sibling_pattern_remains(text_after: str, patch: CandidatePatch) -> bool:
    """True if the pre-image snippet still occurs after a one-site apply (E7).

    Mechanism-level: same ``original_lines`` string still present — no symbol names.
    """
    original = (patch.original_lines or "").strip("\n")
    if not original or not (patch.patched_lines or "").strip("\n"):
        return False
    if original == (patch.patched_lines or "").strip("\n"):
        return False
    return original in text_after


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


def _collapse_ws(s: str) -> str:
    """折叠内部空白，容忍 tab/多空格差异。"""
    return re.sub(r"[ \t]+", " ", s.strip())


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


def _replace_all_lines_by_strip(text: str, old_line: str, new_line: str) -> str | None:
    """单行 strip 匹配的全部命中替换（E6a sibling）。"""
    if "\n" in old_line.strip("\n"):
        return None
    current = text
    changed = False
    for _ in range(64):
        nxt = _replace_line_by_strip(current, old_line, new_line)
        if nxt is None:
            break
        changed = True
        current = nxt
    return current if changed else None


def _replace_multiline_by_strip_keys(
    text: str, old_block: str, new_block: str
) -> str | None:
    """多行块：按每行 strip 键在文件中找连续匹配窗口并替换（容忍缩进/尾空白差）。"""
    old_lines = [ln.rstrip("\r\n") for ln in old_block.splitlines()]
    new_lines = [ln.rstrip("\r\n") for ln in new_block.splitlines()]
    if len(old_lines) < 2:
        return None
    old_keys = [_line_match_key(ln) for ln in old_lines]
    if not all(old_keys):
        return None

    file_lines = text.splitlines(keepends=True)
    n = len(old_keys)
    for start in range(0, len(file_lines) - n + 1):
        window = [file_lines[start + i].rstrip("\n\r") for i in range(n)]
        if [_line_match_key(w) for w in window] != old_keys:
            continue
        return _rebuild_block_at(file_lines, start, n, window, new_lines)
    return None


def _rebuild_block_at(
    file_lines: list[str],
    start: int,
    n: int,
    window: list[str],
    new_lines: list[str],
) -> str:
    indent0 = window[0][: len(window[0]) - len(window[0].lstrip())]
    rebuilt: list[str] = []
    for j, nl in enumerate(new_lines):
        ending = (
            file_lines[start + min(j, n - 1)][
                len(file_lines[start + min(j, n - 1)].rstrip("\n\r")) :
            ]
            if file_lines[start + min(j, n - 1)].endswith(("\n", "\r"))
            else "\n"
        )
        body = nl
        if j == 0 and indent0 and not body[:1].isspace() and body.strip():
            if not body.startswith((" ", "\t")):
                body = indent0 + body.lstrip()
        rebuilt.append(body + (ending if ending else "\n"))
    out = file_lines[:start] + rebuilt + file_lines[start + n :]
    return "".join(out)


def _replace_by_collapsed_whitespace(
    text: str, old_block: str, new_block: str
) -> str | None:
    """按折叠空白后的行键匹配并全部替换窗口（E6a′）。"""
    old_lines = [ln.rstrip("\r\n") for ln in old_block.splitlines()]
    new_lines = [ln.rstrip("\r\n") for ln in new_block.splitlines()]
    if not old_lines:
        return None
    old_keys = [_collapse_ws(_line_match_key(ln)) for ln in old_lines]
    if not any(old_keys):
        return None

    file_lines = text.splitlines(keepends=True)
    n = len(old_keys)
    if n > len(file_lines):
        return None

    matches: list[int] = []
    for start in range(0, len(file_lines) - n + 1):
        window = [file_lines[start + i].rstrip("\n\r") for i in range(n)]
        if [_collapse_ws(_line_match_key(w)) for w in window] != old_keys:
            continue
        matches.append(start)
    if not matches:
        return None

    # 从后往前替换，避免索引偏移
    for start in reversed(matches):
        window = [file_lines[start + i].rstrip("\n\r") for i in range(n)]
        indent0 = window[0][: len(window[0]) - len(window[0].lstrip())]
        rebuilt: list[str] = []
        for j, nl in enumerate(new_lines):
            ending = (
                file_lines[start + min(j, n - 1)][
                    len(file_lines[start + min(j, n - 1)].rstrip("\n\r")) :
                ]
                if file_lines[start + min(j, n - 1)].endswith(("\n", "\r"))
                else "\n"
            )
            body = nl
            if j == 0 and indent0 and body.strip() and not body.startswith((" ", "\t")):
                body = indent0 + body.lstrip()
            rebuilt.append(body + (ending if ending else "\n"))
        file_lines = file_lines[:start] + rebuilt + file_lines[start + n :]
    return "".join(file_lines)


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

    replaced = _replace_multiline_by_strip_keys(text, old_block, new_block)
    if replaced is not None:
        return replaced
    replaced = _replace_by_collapsed_whitespace(text, old_block, new_block)
    if replaced is not None:
        return replaced

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
    """Compatibility wrapper over the canonical structured recovery pipeline."""
    from agent_runtime.json_recovery import repair_structured_output

    parsed = repair_structured_output(text)
    return parsed.repaired_text or str(text or "").strip()


class PatchApplier:
    """Parse and apply candidate patches under a repo root."""

    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.last_sibling_warnings: list[str] = []
        self.last_apply_errors: list[str] = []

    def resolve_repo_file(self, file_path: str) -> Path | None:
        from src.repair.path_resolve import resolve_repo_file as _resolve

        return _resolve(self.repo_root, file_path)

    def apply_patches(
        self,
        patches: list[CandidatePatch],
        *,
        allowed_paths: set[str] | None = None,
    ) -> list[CandidatePatch]:
        from src.repair.path_resolve import resolve_repo_relpath

        applied: list[CandidatePatch] = []
        sibling_warnings: list[str] = []
        apply_errors: list[str] = []
        normalized_allowed = {
            str(path).replace("\\", "/").lstrip("./")
            for path in (allowed_paths or set())
        }
        snapshots: dict[str, tuple[bytes, int]] = {}

        def rollback() -> None:
            for rel, (data, mode) in snapshots.items():
                target = self.resolve_repo_file(rel)
                if target is None:
                    continue
                try:
                    target.write_bytes(data)
                    target.chmod(mode)
                except OSError:
                    pass

        for p in patches:
            rel = resolve_repo_relpath(self.repo_root, p.file_path)
            file_path = self.resolve_repo_file(p.file_path)
            if file_path is None or rel is None:
                reason = f"path_not_in_repo:{p.file_path}"
                apply_errors.append(reason)
                print(
                    f"  [patcher] ⚠ 拒绝补丁（路径不在 repo 或文件不存在）: {p.file_path!r}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if normalized_allowed and rel not in normalized_allowed:
                apply_errors.append(f"path_not_allowlisted:{rel}")
                continue
            if rel not in snapshots:
                try:
                    snapshots[rel] = (file_path.read_bytes(), file_path.stat().st_mode)
                except OSError as exc:
                    apply_errors.append(f"snapshot_failed:{rel}:{exc}")
                    continue
            expected_hash = str(getattr(p, "base_sha256", "") or "").strip().lower()
            if expected_hash:
                actual_hash = hashlib.sha256(snapshots[rel][0]).hexdigest()
                if actual_hash != expected_hash:
                    apply_errors.append(f"stale_patch:{rel}")
                    continue
            if rel != (p.file_path or "").replace("\\", "/").lstrip("./"):
                p.file_path = rel
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                apply_errors.append(f"read_failed:{rel}:{exc}")
                continue
            new_text = apply_patch_to_text(text, p)
            if new_text is None:
                reason = describe_hunk_mismatch(text, p)
                apply_errors.append(reason)
                print(
                    f"  [patcher] ⚠ 无法应用补丁: {p.file_path} ({reason[:160]})",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            new_text = _sync_import_symbol_usages(text, new_text, p)
            if sibling_pattern_remains(new_text, p):
                msg = (
                    f"{p.file_path}: original_lines still present after one-site apply "
                    "(possible incomplete sibling pattern)"
                )
                sibling_warnings.append(msg)
                print(f"  [patcher] ⚠ {msg}", file=sys.stderr, flush=True)
            from agent_runtime.atomic_io import atomic_write_text

            atomic_write_text(file_path, new_text)
            applied.append(p)
        self.last_sibling_warnings = sibling_warnings
        self.last_apply_errors = apply_errors
        if apply_errors:
            rollback()
            try:
                from agent_runtime.metrics import get_registry

                reason = "stale_patch" if any("stale_patch" in e for e in apply_errors) else "apply_error"
                metric = (
                    "fixloop_stale_patch_rejections_total"
                    if reason == "stale_patch"
                    else "fixloop_patch_rollbacks_total"
                )
                get_registry().counter_inc(metric, labels={"reason": reason})
            except Exception:
                pass
            applied = []
        return applied


def parse_patches(answer: str) -> list[CandidatePatch]:
    from agent_runtime.json_recovery import repair_structured_output

    parsed = repair_structured_output(answer, mode="patch")
    if not parsed.ok:
        return []
    try:
        return [CandidatePatch.from_dict(item) for item in parsed.value]
    except (KeyError, TypeError, ValueError):
        return []
