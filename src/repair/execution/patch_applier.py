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
        return f"hunk_mismatch:{path}: wanted `{preview}` near=[" + " | ".join(near) + "]"
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


def _line_match_key(line: str) -> str:
    """比较行内容时忽略注释与首尾空白。"""
    return line.split("#", 1)[0].strip()


def _collapse_ws(s: str) -> str:
    """折叠内部空白，容忍 tab/多空格差异。"""
    return re.sub(r"[ \t]+", " ", s.strip())


def _statement_anchor(line: str) -> str:
    stripped = _line_match_key(line)
    if stripped.startswith("return "):
        return "return "
    assign = re.match(r"([A-Za-z_][\w.]*\s*=)", stripped)
    return assign.group(1) if assign else ""


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
            str(path).replace("\\", "/").lstrip("./") for path in (allowed_paths or set())
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

                reason = (
                    "stale_patch"
                    if any("stale_patch" in e for e in apply_errors)
                    else "apply_error"
                )
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
